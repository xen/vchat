import logging
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
from aiohttp_tus.utils import parse_upload_metadata
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer

from jobs.crawler import crawl_all_sources_task, crawl_file_task, crawl_source_task
from jobs.embedder.tasks import index_project, refresh_project_index, refresh_source_index
from jobs.suggestions import generate_project_topics
from vchat.ai_providers import (
    DEFAULT_OPENAI_MODEL,
    get_ai_provider_options,
    get_default_model_id,
    is_model_available,
    is_provider_available,
    resolve_ai_settings,
)
from vchat.app_keys import SETTINGS_KEY, SIGNER_KEY
from vchat.document_types import DEFAULT_DOCUMENT_TYPE
from vchat.i18n import _
from vchat.models import Chat, ChatMsg, Chunk, Document, Source
from vchat.project_settings import (
    apply_settings_updates,
    get_setting,
    get_setting_json,
)
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_USER_AGENT,
)
from vchat.settings import config
from vchat.utils import flash, login_required, meta

from . import forms

logger = logging.getLogger(__name__)

__all__ = [
    "index",
    "project_edit",
    "project_action",
    "project_edit_sources",
    "project_source_edit",
    "project_source_settings",
    "project_view",
    "project_document_content",
    "project_documents_json",
    "project_chat",
    "project_stats",
    "project_topics",
    "project_integration",
    "public_widget_chat",
    "project_files",
    "secure_download",
    "delete_file",
    "on_upload",
]


def _project_context(request) -> SimpleNamespace:
    settings = request.app.get(SETTINGS_KEY, {})
    return SimpleNamespace(
        id="global",
        title=settings.get("project.title") or "vchat",
        provider=settings.get("project.provider") or "openai",
        model=settings.get("project.model") or DEFAULT_OPENAI_MODEL,
        system_prompt=settings.get("project.system_prompt") or forms.DEFAULT_SYSTEM_PROMPT,
        agent_style=settings.get("project.agent_style") or "",
        config={
            "agent_name": settings.get("project.agent_name") or "",
            "welcome_message": settings.get("project.welcome_message") or "",
            "secret": settings.get("project.secret") or "",
        },
        meta={
            "topics": get_setting_json(request.app, "project.topics", []),
            "intents": get_setting_json(request.app, "project.intents", []),
        },
    )


def _get_topics(request) -> list[str]:
    topics = get_setting_json(request.app, "project.topics", [])
    return topics if isinstance(topics, list) else []


def _get_intents(request) -> list[str]:
    intents = get_setting_json(request.app, "project.intents", [])
    return intents if isinstance(intents, list) else []


@meta(title=_("Data"))
@login_required()
@aiohttp_jinja2.template("projects/view.html")
async def index(request):
    return await project_view(request)


@meta(title="Настройки проекта")
@login_required()
@aiohttp_jinja2.template("projects/edit.html")
async def project_edit(request):
    db_session = request["db"]
    session = await get_session(request)
    data = await request.post()

    project = _project_context(request)
    form_kwargs = {"meta": {"csrf_context": session}}
    if data:
        form_kwargs["formdata"] = data
    else:
        form_kwargs["data"] = {
            "title": project.title,
            "system_prompt": project.system_prompt,
            "agent_style": project.agent_style,
            "provider": project.provider,
            "model": project.model,
            "agent_name": project.config.get("agent_name", ""),
            "welcome_message": project.config.get("welcome_message", ""),
        }

    form = forms.WorkspaceForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        await apply_settings_updates(
            request.app,
            db_session,
            {
                "project.title": form.title.data,
                "project.system_prompt": form.system_prompt.data,
                "project.agent_style": form.agent_style.data,
                "project.provider": form.provider.data,
                "project.model": form.model.data,
                "project.agent_name": (form.agent_name.data or "").strip(),
                "project.welcome_message": (form.welcome_message.data or "").strip(),
            },
        )
        await db_session.commit()
        await flash(request, _("Settings updated"), "success")
        raise web.HTTPFound(request.app.router["project_edit"].url_for())

    return {
        "form": form,
        "project": project,
        "is_owner": True,
        "ai_provider_options": get_ai_provider_options(),
    }


@meta(title=_("Sources"))
@login_required()
@aiohttp_jinja2.template("projects/sources.html")
async def project_edit_sources(request):
    db_session = request["db"]

    stmt = (
        sa.select(Source, sa.func.count(Document.id).label("doc_count"))
        .outerjoin(Document, Document.source_id == Source.id)
        .group_by(Source.id)
        .order_by(Source.created_at.desc())
    )
    sources = (await db_session.execute(stmt)).all()

    session = await get_session(request)
    form = forms.SourceForm(meta={"csrf_context": session})
    return {"project": _project_context(request), "sources": sources, "form": form}


@meta(title=_("Edit Source"))
@login_required()
@aiohttp_jinja2.template("projects/source_edit.html")
async def project_source_edit(request):
    source_id = int(request.match_info.get("source_id"))
    source = await request["db"].scalar(sa.select(Source).where(Source.id == source_id))
    if not source:
        raise web.HTTPNotFound()

    session = await get_session(request)
    data = await request.post()

    form_kwargs = {"meta": {"csrf_context": session}, "obj": source}
    if data:
        form_kwargs["formdata"] = data
    else:
        form_data = {
            "type": source.type,
            "title": source.title,
            "reindex_period": source.reindex_period,
        }
        source_config = source.config or {}
        if source.type == "s3":
            form_data.update(
                {
                    "aws_access_key_id": source_config.get("aws_access_key_id", ""),
                    "aws_secret_access_key": source_config.get("aws_secret_access_key", ""),
                    "bucket_name": source_config.get("bucket_name", ""),
                    "endpoint_url": source_config.get("endpoint_url", "https://s3.amazonaws.com"),
                    "region": source_config.get("region", "us-east-1"),
                    "prefix": source_config.get("prefix", ""),
                }
            )
        elif source.type == "google_drive":
            form_data.update(
                {
                    "google_drive_folder_id": source_config.get("folder_id", ""),
                    "google_drive_folder_name": source_config.get("folder_name", ""),
                }
            )
        else:
            form_data["url"] = source.uri
        form_kwargs["data"] = form_data

    form = forms.SourceForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        source_type = form.type.data
        source.type = source_type
        source.title = form.title.data
        source.reindex_period = form.reindex_period.data
        source_config = dict(source.config or {})
        crawler_settings = {
            "crawler_concurrent_requests": source_config.get("crawler_concurrent_requests"),
            "crawler_download_delay": source_config.get("crawler_download_delay"),
            "crawler_user_agent": source_config.get("crawler_user_agent"),
        }

        if source_type == "s3":
            new_config = {
                "aws_access_key_id": form.aws_access_key_id.data,
                "aws_secret_access_key": form.aws_secret_access_key.data,
                "bucket_name": form.bucket_name.data,
                "endpoint_url": form.endpoint_url.data or "",
                "region": form.region.data or "us-east-1",
                "prefix": form.prefix.data or "",
            }
            for key, value in crawler_settings.items():
                if value is not None:
                    new_config[key] = value
            source.config = new_config
            source.uri = f"s3://{form.bucket_name.data}"
        elif source_type == "google_drive":
            new_config = {
                "folder_id": form.google_drive_folder_id.data,
                "folder_name": form.google_drive_folder_name.data,
            }
            for key, value in crawler_settings.items():
                if value is not None:
                    new_config[key] = value
            source.config = new_config
            source.uri = f"gdrive://{form.google_drive_folder_id.data}"
        else:
            source.uri = form.url.data
            rule_types = data.getall("rule_type[]", [])
            rule_values = data.getall("rule_value[]", [])
            rules = []
            for r_type, r_value in zip(rule_types, rule_values):
                if r_value.strip():
                    rules.append({"type": r_type, "value": r_value.strip()})
            new_config = source_config.copy()
            if rules:
                new_config["rules"] = rules
            elif "rules" in new_config:
                del new_config["rules"]
            source.config = new_config

        await request["db"].commit()
        await flash(request, _("Source updated"), "success")
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    return {"project": _project_context(request), "source": source, "form": form}


@meta(title=_("Source Settings"))
@login_required()
@aiohttp_jinja2.template("projects/source_settings.html")
async def project_source_settings(request):
    source_id = int(request.match_info.get("source_id"))
    db_session = request["db"]
    source = await db_session.scalar(sa.select(Source).where(Source.id == source_id))
    if not source:
        raise web.HTTPNotFound()

    session = await get_session(request)
    form_kwargs = {"meta": {"csrf_context": session}}
    if request.method == "POST":
        data = await request.post()
        form_kwargs["formdata"] = data
    else:
        source_config = source.config or {}
        form_kwargs["data"] = {
            "reindex_period": source.reindex_period,
            "concurrent_requests": source_config.get(
                "crawler_concurrent_requests", DEFAULT_CRAWLER_CONCURRENT_REQUESTS
            ),
            "download_delay": source_config.get(
                "crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY
            ),
            "user_agent": source_config.get("crawler_user_agent") or DEFAULT_CRAWLER_USER_AGENT,
        }

    form = forms.SourceCrawlerSettingsForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        source_config = dict(source.config or {})
        source.reindex_period = form.reindex_period.data
        source.updated_at = datetime.now(timezone.utc)
        source_config["crawler_user_agent"] = (
            (form.user_agent.data or "").strip() or DEFAULT_CRAWLER_USER_AGENT
        )
        source_config["crawler_concurrent_requests"] = int(
            form.concurrent_requests.data
            if form.concurrent_requests.data is not None
            else DEFAULT_CRAWLER_CONCURRENT_REQUESTS
        )
        source_config["crawler_download_delay"] = float(
            form.download_delay.data
            if form.download_delay.data is not None
            else DEFAULT_CRAWLER_DOWNLOAD_DELAY
        )

        source.config = source_config
        await db_session.commit()
        await flash(request, _("Source settings updated"), "success")
        raise web.HTTPFound(request.path)

    return {"project": _project_context(request), "source": source, "form": form}


@meta(title=_("Topics"))
@login_required()
@aiohttp_jinja2.template("projects/topics.html")
async def project_topics(request):
    db_session = request["db"]
    session = await get_session(request)

    form_kwargs = {"meta": {"csrf_context": session}}
    if request.method == "POST":
        data = await request.post()
        form_kwargs["formdata"] = data
    else:
        form_kwargs["data"] = {
            "topics": "\n".join(_get_topics(request)),
            "intents": "\n".join(_get_intents(request)),
        }

    form = forms.TopicsForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        topics_list = [t.strip() for t in form.topics.data.split("\n") if t.strip()]
        intents_list = [i.strip() for i in form.intents.data.split("\n") if i.strip()]
        await apply_settings_updates(
            request.app,
            db_session,
            {
                "project.topics": topics_list,
                "project.intents": intents_list,
            },
        )
        await db_session.commit()
        await flash(request, _("Topics updated"), "success")
        raise web.HTTPFound(request.path)

    return {"project": _project_context(request), "form": form}


@login_required()
async def project_action(request):
    db_session = request["db"]
    item_id = request.match_info.get("item_id")
    action = request.match_info.get("action")
    user_id = request["user"].id

    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    try:
        signed_user_id = request.app[SIGNER_KEY].loads(token, max_age=86400)
        if signed_user_id != user_id:
            raise web.HTTPForbidden(text="Invalid CSRF Token Owner")
    except (BadSignature, SignatureExpired):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    if action == "update_ai_settings":
        data = await request.post()
        provider = (data.get("provider") or "").strip()
        model = (data.get("model") or "").strip()

        if not provider or not is_provider_available(provider):
            raise web.HTTPBadRequest(text="Unknown provider")
        if not model or not is_model_available(provider, model):
            model = get_default_model_id(provider)

        await apply_settings_updates(
            request.app,
            db_session,
            {
                "project.provider": provider,
                "project.model": model,
            },
        )
        await db_session.commit()

        if request.headers.get("HX-Request"):
            provider_obj, model_obj = resolve_ai_settings(provider, model)
            return aiohttp_jinja2.render_template(
                "chat/includes/ai_settings.html",
                request,
                {
                    "project": _project_context(request),
                    "ai_provider_options": get_ai_provider_options(),
                    "current_ai_provider": provider_obj.id,
                    "current_ai_model": model_obj.id,
                    "ai_settings_url": request.app.router["project_actions"].url_for(
                        action="update_ai_settings", item_id="global"
                    ),
                    "allow_ai_switch": True,
                },
            )
        return web.json_response({"ok": True, "provider": provider, "model": model})

    if action == "generate_topics":
        generate_project_topics.delay()
        await flash(request, _("Topics generation started in background"), "success")
        return web.json_response({"ok": True})

    if action == "reset_secret":
        secret = secrets.token_urlsafe(32)
        await apply_settings_updates(
            request.app,
            db_session,
            {"project.secret": secret},
        )
        await db_session.commit()
        return aiohttp_jinja2.render_template(
            "projects/_integration_secret_field.html",
            request,
            {"project": _project_context(request), "project_secret": secret},
        )

    if action == "delete_document":
        document = await db_session.scalar(sa.select(Document).where(Document.id == int(item_id)))
        if not document:
            raise web.HTTPNotFound(text="Document not found")
        await db_session.delete(document)
        await db_session.commit()
        response = web.Response(text="")
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    if action == "ignore_document":
        document = await db_session.scalar(sa.select(Document).where(Document.id == int(item_id)))
        if not document:
            raise web.HTTPNotFound(text="Document not found")
        data = await request.post()
        raw_value = data.get("is_ignored")
        if raw_value is not None:
            document.is_ignored = str(raw_value).lower() in {"1", "true", "yes", "on"}
        else:
            document.is_ignored = not bool(document.is_ignored)
        await db_session.commit()
        response = web.json_response({"is_ignored": document.is_ignored})
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    if action == "add_source":
        data = await request.post()
        session = await get_session(request)
        form = forms.SourceForm(data, meta={"csrf_context": session})
        if not form.validate():
            return web.Response(text="Error", status=400)

        title = form.title.data
        source_type = form.type.data
        reindex_period = form.reindex_period.data
        source_config = {}

        if source_type == "s3":
            source_config = {
                "aws_access_key_id": form.aws_access_key_id.data,
                "aws_secret_access_key": form.aws_secret_access_key.data,
                "bucket_name": form.bucket_name.data,
                "endpoint_url": form.endpoint_url.data or "",
                "region": form.region.data or "us-east-1",
                "prefix": form.prefix.data or "",
            }
            uri = f"s3://{form.bucket_name.data}"
            if not title:
                title = form.bucket_name.data
        elif source_type == "google_drive":
            source_config = {
                "folder_id": form.google_drive_folder_id.data,
                "folder_name": form.google_drive_folder_name.data,
            }
            if session.get("google_refresh_token"):
                source_config["refresh_token"] = session.get("google_refresh_token")
            uri = f"gdrive://{form.google_drive_folder_id.data}"
            if not title:
                title = form.google_drive_folder_name.data or "Google Drive"
        else:
            uri = form.url.data
            if not title:
                from urllib.parse import urlparse

                parsed_uri = urlparse(form.url.data)
                title = parsed_uri.netloc or parsed_uri.path
            rule_types = data.getall("rule_type[]", [])
            rule_values = data.getall("rule_value[]", [])
            rules = []
            for r_type, r_value in zip(rule_types, rule_values):
                if r_value.strip():
                    rules.append({"type": r_type, "value": r_value.strip()})
            if rules:
                source_config["rules"] = rules

        source = Source(
            type=source_type,
            uri=uri,
            title=title,
            config=source_config,
            reindex_period=reindex_period,
        )
        db_session.add(source)
        await db_session.commit()
        crawl_source_task.delay(source.id)
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "delete_source":
        source = await db_session.scalar(sa.select(Source).where(Source.id == int(item_id)))
        if not source:
            raise web.HTTPNotFound()
        await db_session.delete(source)
        await db_session.commit()
        return web.Response(text="", status=200)

    if action == "rebuild_source":
        source = await db_session.scalar(sa.select(Source).where(Source.id == int(item_id)))
        if not source:
            raise web.HTTPNotFound()
        await db_session.execute(sa.delete(Document).where(Document.source_id == source.id))
        await db_session.commit()
        crawl_source_task.delay(source.id)
        return web.Response(text="ok")

    if action == "crawl_source":
        source = await db_session.scalar(sa.select(Source).where(Source.id == int(item_id)))
        if not source:
            raise web.HTTPNotFound()
        crawl_source_task.delay(source.id)
        await flash(request, _("Crawl task started for source"), "success")
        return web.Response(text="ok", status=200)

    if action == "refresh_source_index":
        source = await db_session.scalar(sa.select(Source).where(Source.id == int(item_id)))
        if not source:
            raise web.HTTPNotFound()
        refresh_source_index.delay(source.id)
        await flash(request, _("Update task started for %(title)s", title=source.title or source.uri), "success")
        return web.Response(text="ok", status=200)

    if action == "crawl_all":
        crawl_all_sources_task.delay()
        await flash(request, _("Crawl task started for all sources"), "success")
        return web.Response(text="ok", status=200)

    if action == "refresh_project_index":
        refresh_project_index.delay()
        await flash(request, _("Update task started"), "success")
        return web.Response(text="ok", status=200)

    if action == "index_project":
        index_project.delay()
        await flash(request, _("Full rebuild task started"), "success")
        return web.Response(text="ok", status=200)

    if action == "rebuild_uploads":
        upload_source = await db_session.scalar(
            sa.select(Source).where(Source.type == "upload").order_by(Source.id.asc())
        )
        if upload_source:
            refresh_source_index.delay(upload_source.id)
        await flash(request, _("Upload index rebuild started"), "success")
        return web.Response(text="ok", status=200)

    raise web.HTTPBadRequest(text="Unknown action")


@meta(title=_("Data"))
@login_required()
@aiohttp_jinja2.template("projects/view.html")
async def project_view(request):
    db_session = request["db"]
    sources = (
        (
            await db_session.execute(
                sa.select(Source).order_by(Source.title.asc(), Source.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {"project": _project_context(request), "sources": sources}


@login_required()
async def project_documents_json(request):
    chunk_counts = (
        sa.select(
            Chunk.document_id.label("document_id"),
            sa.func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.document_id)
        .subquery()
    )

    size_bytes_expr = sa.func.coalesce(
        sa.cast(sa.func.octet_length(Document.content), sa.BigInteger),
        sa.cast(Document._length, sa.BigInteger),
        sa.literal(0, type_=sa.BigInteger),
    ).label("size_bytes")

    documents = (
        await request["db"].execute(
            sa.select(
                Document,
                Source,
                size_bytes_expr,
                chunk_counts.c.chunk_count,
            )
            .join(Source, Document.source_id == Source.id)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
            .order_by(Document.created_at.desc())
        )
    ).all()

    data = []
    for doc, source, size_bytes, chunk_count in documents:
        raw_meta = doc.meta if isinstance(doc.meta, dict) else {}
        meta_payload = dict(raw_meta)

        doc_type_value = meta_payload.get("doc_type")
        if not isinstance(doc_type_value, str) or not doc_type_value:
            doc_type_value = DEFAULT_DOCUMENT_TYPE
        meta_payload.setdefault("doc_type", doc_type_value)

        data.append(
            {
                "id": str(doc.id),
                "title": doc.title or (doc.uri.split("/")[-1] if doc.uri else "Без названия"),
                "source": source.title or source.uri,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
                "status": doc.status,
                "is_ignored": doc.is_ignored,
                "uri": doc.uri,
                "size_bytes": int(size_bytes or 0),
                "chunk_count": int(chunk_count or 0),
                "document_type": doc_type_value,
                "meta": meta_payload,
            }
        )

    return web.json_response(data)


@meta(title=_("Stats"))
@login_required()
@aiohttp_jinja2.template("projects/stats.html")
async def project_stats(request):
    db = request["db"]

    from datetime import datetime, timedelta

    start_date = datetime.utcnow() - timedelta(days=30)

    chats_query = (
        sa.select(
            sa.func.date_trunc("day", Chat.created_at).label("day"),
            sa.func.count(Chat.id).label("count"),
            sa.func.count(sa.distinct(Chat.user_uid)).label("users"),
        )
        .where(Chat.created_at >= start_date)
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    chats_res = (await db.execute(chats_query)).all()

    msgs_query = (
        sa.select(
            sa.func.date_trunc("day", ChatMsg.created_at).label("day"),
            sa.func.count(ChatMsg.id).label("count"),
            sa.func.sum(sa.func.jsonb_array_length(ChatMsg.used_chunks)).label("hits"),
            sa.func.sum(ChatMsg.tokens).label("tokens"),
        )
        .where(ChatMsg.created_at >= start_date, ChatMsg.role == "assistant")
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    msgs_res = (await db.execute(msgs_query)).all()

    token_usage_query = (
        sa.select(
            ChatMsg.provider,
            ChatMsg.model,
            sa.func.sum(ChatMsg.tokens).label("tokens"),
        )
        .where(ChatMsg.role == "assistant")
        .group_by(ChatMsg.provider, ChatMsg.model)
    )
    token_usage_res = (await db.execute(token_usage_query)).all()

    all_providers = get_ai_provider_options(include_disabled=True)
    provider_labels = {item["id"]: item["title"] for item in all_providers}
    model_labels = {}
    for item in all_providers:
        for model in item.get("models", []):
            model_labels[(item["id"], model["id"])] = model["label"]

    token_breakdown = []
    for row in token_usage_res:
        provider_key = row.provider or "openai"
        model_name = row.model or DEFAULT_OPENAI_MODEL
        token_breakdown.append(
            {
                "provider": provider_key,
                "provider_label": provider_labels.get(provider_key, provider_key.capitalize()),
                "model": model_name,
                "model_label": model_labels.get((provider_key, model_name), model_name),
                "tokens": row.tokens or 0,
            }
        )
    token_breakdown.sort(key=lambda item: item["tokens"], reverse=True)

    stats = {}
    for i in range(31):
        d = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        stats[d] = {"chats": 0, "users": 0, "messages": 0, "hits": 0, "tokens": 0}

    for row in chats_res:
        d = row.day.strftime("%Y-%m-%d")
        if d in stats:
            stats[d]["chats"] = row.count
            stats[d]["users"] = row.users

    for row in msgs_res:
        d = row.day.strftime("%Y-%m-%d")
        if d in stats:
            stats[d]["messages"] = row.count
            stats[d]["hits"] = row.hits or 0
            stats[d]["tokens"] = row.tokens or 0

    labels = sorted(stats.keys())
    data_chats = [stats[d]["chats"] for d in labels]
    data_users = [stats[d]["users"] for d in labels]
    data_msgs = [stats[d]["messages"] for d in labels]
    data_hits = [stats[d]["hits"] for d in labels]
    data_tokens = [stats[d]["tokens"] for d in labels]

    total_unique_users = (
        await db.scalar(
            sa.select(sa.func.count(sa.distinct(Chat.user_uid))).where(
                Chat.created_at >= start_date
            )
        )
        or 0
    )

    source_docs_query = (
        sa.select(
            Source.id,
            Source.title,
            sa.func.count(Document.id).label("doc_count"),
            sa.func.coalesce(sa.func.sum(Document._length), 0).label("data_volume"),
        )
        .select_from(Source)
        .outerjoin(Document, Document.source_id == Source.id)
        .group_by(Source.id, Source.title)
        .order_by(Source.title)
    )
    source_docs_res = (await db.execute(source_docs_query)).all()

    source_chunks_query = (
        sa.select(
            Source.id,
            sa.func.count(Chunk.id).label("chunk_count"),
            sa.func.coalesce(sa.func.sum(sa.func.length(Chunk.content)), 0).label("chunk_storage"),
        )
        .select_from(Source)
        .outerjoin(Document, Document.source_id == Source.id)
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .group_by(Source.id)
    )
    source_chunks_res = (await db.execute(source_chunks_query)).all()

    chunks_by_source = {row.id: row for row in source_chunks_res}
    source_stats = []
    total_docs = 0
    total_data_volume = 0
    total_chunks = 0
    total_chunk_storage = 0

    for row in source_docs_res:
        chunk_data = chunks_by_source.get(row.id)
        chunk_count = chunk_data.chunk_count if chunk_data else 0
        chunk_storage = chunk_data.chunk_storage if chunk_data else 0
        source_stats.append(
            {
                "id": row.id,
                "title": row.title,
                "doc_count": row.doc_count,
                "data_volume": row.data_volume,
                "chunk_count": chunk_count,
                "chunk_storage": chunk_storage,
            }
        )
        total_docs += row.doc_count
        total_data_volume += row.data_volume
        total_chunks += chunk_count
        total_chunk_storage += chunk_storage

    return {
        "project": _project_context(request),
        "labels": labels,
        "data_chats": data_chats,
        "data_users": data_users,
        "data_msgs": data_msgs,
        "data_hits": data_hits,
        "data_tokens": data_tokens,
        "total_chats": sum(data_chats),
        "total_users": total_unique_users,
        "total_msgs": sum(data_msgs),
        "total_hits": sum(data_hits),
        "total_tokens": sum(data_tokens),
        "token_breakdown": token_breakdown,
        "source_stats": source_stats,
        "total_docs": total_docs,
        "total_data_volume": total_data_volume,
        "total_chunks": total_chunks,
        "total_chunk_storage": total_chunk_storage,
    }


@login_required()
async def project_document_content(request):
    document_id = int(request.match_info.get("document_id"))
    document = await request["db"].scalar(sa.select(Document).where(Document.id == document_id))
    if not document:
        raise web.HTTPNotFound()

    return web.Response(
        text=f'<pre class="whitespace-pre-wrap overflow-y-auto" style="max-height: 500px">{document.content}</pre>',
        content_type="text/html",
    )


@meta(title=_("Chat"))
@login_required()
@aiohttp_jinja2.template("chat/chat.html")
async def project_chat(request):
    chat_id = (request.match_info.get("chat_id") or "").strip()
    if chat_id:
        chat = await request["db"].scalar(sa.select(Chat).where(Chat.id == chat_id))
        if not chat:
            raise web.HTTPNotFound(text="Chat not found")
    else:
        user_uid_param = request.rel_url.query.get("user_uid", "").strip()
        user_uid = user_uid_param or str(request["user"].id)

        project = _project_context(request)
        chat = Chat(
            title=f"Chat for {project.title}",
            user_uid=user_uid,
            meta={},
            type="chat",
        )
        request["db"].add(chat)
        await request["db"].commit()
        await request["db"].refresh(chat)
        location = request.app.router["project_chat_with_id"].url_for(chat_id=chat.id)
        raise web.HTTPFound(location=location)

    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([request["user"].id, chat.id], salt="vchat")
    history_rows = (
        await request["db"].execute(
            sa.select(ChatMsg)
            .where(ChatMsg.chat_id == chat.id)
            .order_by(ChatMsg.created_at.asc(), ChatMsg.id.asc())
        )
    ).scalars().all()
    initial_messages = []
    for row in history_rows:
        signed_msg_id = None
        if row.role == "assistant":
            signed_msg_id = serializer.dumps(row.id, salt="chat_msg")
        initial_messages.append(
            {
                "role": row.role,
                "content": row.text or "",
                "msg_id": row.id,
                "signed_msg_id": signed_msg_id,
                "vote": row.vote,
            }
        )

    project = _project_context(request)
    provider_obj, model_obj = resolve_ai_settings(project.provider, project.model)
    ai_settings_url = request.app.router["project_actions"].url_for(
        action="update_ai_settings", item_id="global"
    )

    return {
        "project": project,
        "chat": chat,
        "payload": payload,
        "agent_name": project.config.get("agent_name", ""),
        "welcome_message": project.config.get("welcome_message", ""),
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": True,
        "ai_settings_url": str(ai_settings_url),
        "initial_messages": initial_messages,
    }


@meta(title=_("Integration"))
@login_required()
@aiohttp_jinja2.template("projects/integration.html")
async def project_integration(request):
    secret = get_setting(request.app, "project.secret", "") or ""
    if not secret:
        secret = secrets.token_urlsafe(32)
        await apply_settings_updates(request.app, request["db"], {"project.secret": secret})
        await request["db"].commit()

    return {"project": _project_context(request), "project_secret": secret}


async def _render_public_chat(request):
    user_uid = request.query.get("user_uid", "").strip()
    user_name = request.query.get("user_name", "")
    user_email = request.query.get("user_email", "")
    sign = request.query.get("sign", "")

    if not user_uid:
        import uuid

        user_uid = f"guest_{uuid.uuid4().hex[:8]}"

    secret = get_setting(request.app, "project.secret", "") or ""
    if sign and secret:
        import hashlib
        import hmac

        expected_sign = hmac.new(secret.encode("utf-8"), user_uid.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sign, sign):
            return web.HTTPForbidden(text="Invalid signature")

    chat = Chat(
        title=f"Chat for {user_name or user_uid}",
        user_uid=user_uid,
        meta={"name": user_name, "email": user_email},
        type="chat",
    )
    request["db"].add(chat)
    await request["db"].commit()
    await request["db"].refresh(chat)

    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([user_uid, chat.id], salt="vchat")
    support_csrf_token = request.app[SIGNER_KEY].dumps({"chat_id": chat.id})

    project = _project_context(request)
    provider_obj, model_obj = resolve_ai_settings(project.provider, project.model)

    return {
        "project": project,
        "chat": chat,
        "payload": payload,
        "agent_name": project.config.get("agent_name", ""),
        "welcome_message": project.config.get("welcome_message", ""),
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": False,
        "ai_settings_url": None,
        "support_csrf_token": support_csrf_token,
    }


@meta(title=_("Chat Widget"))
@aiohttp_jinja2.template("chat/chat.html")
async def public_widget_chat(request):
    if not (request.app["config"].get("vchat_chat") or "").strip():
        raise web.HTTPNotFound(text="Widget chat is not configured")
    return await _render_public_chat(request)


@meta(title=_("Files"))
@login_required()
@aiohttp_jinja2.template("projects/files.html")
async def project_files(request):
    db_session = request["db"]

    source = await db_session.scalar(
        sa.select(Source).where(Source.type == "upload").order_by(Source.id.asc())
    )
    if not source:
        source = Source(type="upload", title="Uploaded Files", uri="uploads://", config={})
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

    chunk_counts = (
        sa.select(
            Chunk.document_id.label("document_id"),
            sa.func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.document_id)
        .subquery()
    )

    rows = (
        await db_session.execute(
            sa.select(Document, chunk_counts.c.chunk_count)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
            .where(Document.source_id == source.id)
            .order_by(Document.created_at.desc())
        )
    ).all()

    documents = []
    for doc, chunk_count in rows:
        doc.chunk_count = int(chunk_count or 0)
        documents.append(doc)

    return {
        "project": _project_context(request),
        "documents": documents,
        "upload_source": source,
    }


@login_required()
async def secure_download(request):
    file_id = int(request.match_info.get("file_id"))
    db_session = request["db"]

    document = await db_session.scalar(
        sa.select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == file_id, Source.type == "upload")
    )

    if not document:
        raise web.HTTPNotFound()

    file_path = document.uri
    if not os.path.exists(file_path):
        raise web.HTTPNotFound(text="File not found on disk")

    return web.FileResponse(file_path)


@login_required()
async def delete_file(request):
    file_id = int(request.match_info.get("file_id"))
    db_session = request["db"]

    document = await db_session.scalar(
        sa.select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == file_id, Source.type == "upload")
    )

    if document:
        if document.uri and os.path.exists(document.uri):
            try:
                os.remove(document.uri)
            except OSError as e:
                logger.error("Error deleting file %s: %s", document.uri, e)

        await db_session.delete(document)
        await db_session.commit()
        return web.Response(text="ok")

    return web.HTTPNotFound()


async def on_upload(request: web.Request, resource: Any, source_path: Path) -> None:
    db_session = request.get("db")

    source = await db_session.scalar(
        sa.select(Source).where(Source.type == "upload").order_by(Source.id.asc())
    )
    if not source:
        source = Source(type="upload", title="Uploaded Files", uri="uploads://", config={})
        db_session.add(source)
        await db_session.flush()

    metadata = parse_upload_metadata(resource.metadata_header or "")

    def _decode_meta(value: Any | None, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str):
            return value or default
        return str(value)

    filename_meta = _decode_meta(metadata.get("filename"))
    original_filename = filename_meta or resource.file_name or "unknown"
    ext = Path(original_filename).suffix

    document = Document(
        source_id=source.id,
        title=original_filename,
        uri="",
        content="",
        hash_value="",
        meta={
            "filename": original_filename,
            "content_type": _decode_meta(metadata.get("filetype")),
            "doc_type": "file",
        },
        status="added",
    )
    db_session.add(document)
    await db_session.flush()

    uploads_dir = Path("media/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    new_filename = f"{document.id}{ext}"
    target_path = uploads_dir / new_filename

    shutil.move(str(source_path), str(target_path))

    document.uri = str(target_path)
    document.hash_value = str(target_path)
    document.length = 0

    await db_session.commit()
    crawl_file_task.delay(document.id)
