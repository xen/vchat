import logging
import os
import secrets
import json
import hashlib
import hmac
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer
from passlib.context import CryptContext

from jobs.crawler import crawl_all_sources_task, crawl_file_task, crawl_source_task
from jobs.embedder.tasks import (
    index_project,
    index_document,
    refresh_project_index,
    refresh_source_index,
)
from jobs.suggestions import generate_project_topics
from vchat.ai_providers import (
    DEFAULT_OPENAI_MODEL,
    get_ai_provider_options,
    get_default_model_id,
    is_model_available,
    is_provider_available,
    resolve_ai_settings,
)
from vchat.app_keys import CONFIG_KEY, REDIS_KEY, SETTINGS_KEY, SIGNER_KEY
from vchat.chat_meta import merge_chat_meta
from vchat.document_types import DEFAULT_DOCUMENT_TYPE
from vchat.i18n import _
from vchat.models import Chat, ChatMsg, Chunk, Document, Source, User
from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.project_settings import (
    apply_settings_updates,
    get_setting,
    get_setting_json,
)
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_CRAWLER_USER_AGENT,
    MANUAL_REINDEX_MODE,
    normalize_reindex_cron,
)
from vchat.settings import config
from vchat.utils import admin_event, flash, login_required, meta

from vchat.views.admin import forms as admin_forms

from . import forms

logger = logging.getLogger(__name__)
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")

__all__ = [
    "index",
    "project_edit",
    "project_action",
    "project_edit_sources",
    "project_source_settings",
    "project_view",
    "project_document_content",
    "project_document_detail",
    "project_documents_json",
    "project_files_json",
    "project_chat",
    "project_stats",
    "project_topics",
    "project_integration",
    "public_widget_chat",
    "project_files",
    "file_document",
    "project_progress",
    "secure_download",
    "on_upload",
]

EMBED_STATS_KEY = "vchat:embed:chunk_durations"


def _message_sources(row: ChatMsg) -> list[dict[str, Any]]:
    if row.role != "assistant":
        return []

    if row.full_context:
        try:
            payload = json.loads(row.full_context)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("sources"), list):
            return [item for item in payload["sources"] if isinstance(item, dict)]

    sources: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in row.used_chunks or []:
        if not isinstance(item, dict):
            continue
        citation_id = item.get("citation_id")
        uri = item.get("uri")
        title = item.get("title")
        display_path = item.get("display_path") or title
        key = (citation_id, uri, display_path)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "citation_id": citation_id,
                "uri": uri,
                "title": title,
                "display_path": display_path,
                "kind": item.get("kind"),
                "header_text": item.get("header_text"),
                "section_path": item.get("section_path"),
            }
        )
    return sources


def _display_document_title(title: str | None, uri: str | None) -> str:
    cleaned = " ".join((title or "").split()).strip()
    if cleaned:
        lowered = cleaned.lower()
        if (
            len(cleaned) <= 220
            and len(cleaned.split()) <= 30
            and "fatal error" not in lowered
            and "traceback (most recent call last)" not in lowered
            and "exception:" not in lowered
            and "warning:" not in lowered
        ):
            return cleaned

    if uri:
        parsed = urlparse(uri)
        segment = unquote(parsed.path.rstrip("/").split("/")[-1]).strip()
        if segment:
            return segment
        if parsed.netloc:
            return parsed.netloc

    return "Без названия"


async def _document_detail_context(request, document_id: int) -> dict[str, Any]:
    db = request["db"]
    document = await db.scalar(sa.select(Document).where(Document.id == document_id))
    if not document:
        raise web.HTTPNotFound()

    chunk_rows = (
        (
            await db.execute(
                sa.select(Chunk)
                .where(Chunk.document_id == document.id)
                .order_by(Chunk.chunk_ix.asc(), Chunk.id.asc())
            )
        )
        .scalars()
        .all()
    )

    raw_meta = document.meta if isinstance(document.meta, dict) else {}
    structure = (
        raw_meta.get("structure") if isinstance(raw_meta.get("structure"), list) else []
    )
    outline = (
        raw_meta.get("outline") if isinstance(raw_meta.get("outline"), list) else []
    )
    extraction = (
        raw_meta.get("extraction")
        if isinstance(raw_meta.get("extraction"), dict)
        else {}
    )

    return {
        "project": _project_context(request),
        "document": document,
        "document_structure": structure,
        "document_outline": outline,
        "document_extraction": extraction,
        "document_chunks": chunk_rows,
    }


def _project_context(request) -> SimpleNamespace:
    settings = request.app.get(SETTINGS_KEY, {})
    return SimpleNamespace(
        id="global",
        title=settings.get("project.title") or "vchat",
        provider=settings.get("project.provider") or "openai",
        model=settings.get("project.model") or DEFAULT_OPENAI_MODEL,
        system_prompt=settings.get("project.system_prompt")
        or forms.DEFAULT_SYSTEM_PROMPT,
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


async def _files_rows(db_session: Any) -> list[dict[str, Any]]:
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

    rows = (
        await db_session.execute(
            sa.select(Document, size_bytes_expr, chunk_counts.c.chunk_count)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
            .where(Document.source_id.is_(None), Document.uri.is_(None))
            .order_by(Document.updated_at.desc(), Document.created_at.desc())
        )
    ).all()

    result: list[dict[str, Any]] = []
    for doc, size_bytes, chunk_count in rows:
        raw_meta = doc.meta if isinstance(doc.meta, dict) else {}
        author_name = raw_meta.get("author_name")
        if not isinstance(author_name, str) or not author_name.strip():
            author_email = raw_meta.get("author_email")
            if isinstance(author_email, str) and author_email.strip():
                author_name = author_email
            else:
                author_name = "-"

        updated_value = getattr(doc, "updated_at", None) or getattr(
            doc, "created_at", None
        )
        updated_display = (
            updated_value.astimezone().strftime("%d.%m.%Y %H:%M")
            if updated_value
            else "-"
        )
        result.append(
            {
                "document": doc,
                "size_bytes": int(size_bytes or 0),
                "chunk_count": int(chunk_count or 0),
                "author_display": author_name,
                "updated_display": updated_display,
            }
        )
    return result


def _file_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    document = row["document"]
    raw_meta = document.meta if isinstance(document.meta, dict) else {}
    document_type = raw_meta.get("doc_type")
    if not isinstance(document_type, str) or not document_type:
        document_type = DEFAULT_DOCUMENT_TYPE
    title = _display_document_title(
        document.title,
        getattr(document, "uri", None) or raw_meta.get("filename"),
    )
    return {
        "id": str(document.id),
        "title": title,
        "created_at": (
            document.created_at.isoformat()
            if getattr(document, "created_at", None)
            else None
        ),
        "updated_at": (
            document.updated_at.isoformat()
            if getattr(document, "updated_at", None)
            else None
        ),
        "size_bytes": int(row.get("size_bytes") or 0),
        "chunk_count": int(row.get("chunk_count") or 0),
        "author_display": row.get("author_display") or "-",
        "updated_display": row.get("updated_display") or "-",
        "document_type": document_type,
    }


@meta(title=_("Страницы"))
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
    form_kwargs: dict[str, Any] = {"meta": {"csrf_context": session}}
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
    form_kwargs: dict[str, Any] = {"meta": {"csrf_context": session}}
    if request.method == "POST":
        data = await request.post()
        form_kwargs["formdata"] = data
    else:
        cfg = source.config
        form_kwargs["data"] = {
            "title": source.title,
            "reindex_cron": ""
            if source.reindex_cron == MANUAL_REINDEX_MODE
            else source.reindex_cron,
            "url": source.uri,
            "concurrent_requests": cfg.crawler_concurrent_requests,
            "download_delay": cfg.crawler_download_delay,
            "download_timeout": cfg.crawler_download_timeout,
            "user_agent": cfg.crawler_user_agent,
        }

    form = forms.SourceSettingsForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        source.title = form.title.data
        source.reindex_cron = normalize_reindex_cron(form.reindex_cron.data)
        source.updated_at = datetime.now(timezone.utc)
        source.uri = form.url.data
        rule_types = data.getall("rule_type[]", [])
        rule_values = data.getall("rule_value[]", [])
        rules = [
            CrawlerRule(type=rt, value=rv.strip())
            for rt, rv in zip(rule_types, rule_values)
            if rv.strip()
        ]
        source.config = SourceConfig(
            crawler_user_agent=(form.user_agent.data or "").strip()
            or DEFAULT_CRAWLER_USER_AGENT,
            crawler_concurrent_requests=int(
                form.concurrent_requests.data or DEFAULT_CRAWLER_CONCURRENT_REQUESTS
            ),
            crawler_download_delay=int(
                form.download_delay.data
                if form.download_delay.data is not None
                else DEFAULT_CRAWLER_DOWNLOAD_DELAY
            ),
            crawler_download_timeout=int(
                form.download_timeout.data
                if form.download_timeout.data is not None
                else DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
            ),
            rules=rules,
        )

        await db_session.commit()
        await admin_event("source_update", request)
        await flash(request, _("Source settings updated"), "success")
        raise web.HTTPFound(request.path)

    return {"project": _project_context(request), "source": source, "form": form}


@meta(title=_("Topics"))
@login_required()
@aiohttp_jinja2.template("projects/topics.html")
async def project_topics(request):
    db_session = request["db"]
    session = await get_session(request)

    form_kwargs: dict[str, Any] = {"meta": {"csrf_context": session}}
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
        prev_topics = _get_topics(request)
        prev_intents = _get_intents(request)
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
        if (prev_topics or prev_intents) and not topics_list and not intents_list:
            await admin_event("topics_delete", request)
        else:
            await admin_event("topics_update", request)
        await flash(request, _("Topics updated"), "success")
        raise web.HTTPFound(request.path)

    return {"project": _project_context(request), "form": form}


@login_required()
async def project_action(request):
    db_session = request["db"]
    item_id = request.match_info.get("item_id")
    action = request.match_info.get("action")
    user_id = request["user"].id

    if action not in {"user_create", "user_password"}:
        token = request.headers.get("X-CSRFToken")
        if not token:
            raise web.HTTPForbidden(text="Missing CSRF Token")

        try:
            signed_user_id = request.app[SIGNER_KEY].loads(token, max_age=86400)
            if signed_user_id != user_id:
                raise web.HTTPForbidden(text="Invalid CSRF Token Owner")
        except (BadSignature, SignatureExpired):
            raise web.HTTPForbidden(text="Invalid CSRF Token")

    if action == "user_create":
        session = await get_session(request)
        data = await request.post()
        form = admin_forms.CreateUserForm(data, meta={"csrf_context": session})
        users = (
            (await db_session.execute(sa.select(User).order_by(User.id.desc())))
            .scalars()
            .all()
        )

        if not form.validate():
            return aiohttp_jinja2.render_template(
                "admin/user_list.html",
                request,
                {
                    "users": users,
                    "add_form": form,
                    "total_users": len(users),
                    "current_user_id": request["user"].id,
                },
                status=400,
            )

        email = form.email.data.strip().lower()
        exists = await db_session.scalar(sa.select(User.id).where(User.email == email))
        if exists:
            form.email.errors.append(_("This email is already in use"))
            return aiohttp_jinja2.render_template(
                "admin/user_list.html",
                request,
                {
                    "users": users,
                    "add_form": form,
                    "total_users": len(users),
                    "current_user_id": request["user"].id,
                },
                status=400,
            )

        db_session.add(
            User(
                email=email,
                name=(email.split("@", 1)[0] or email).strip()[:100],
                password=password_context.hash(form.password.data),
                is_active=True,
            )
        )
        await db_session.commit()
        await admin_event("user_create", request)
        await flash(request, _("User created"), "success")
        raise web.HTTPFound(request.app.router["users"].url_for())

    if action == "user_password":
        target_user_id = int(item_id)
        user_obj = await db_session.scalar(
            sa.select(User).where(User.id == target_user_id)
        )
        if not user_obj:
            raise web.HTTPNotFound()

        session = await get_session(request)
        data = await request.post() if request.method == "POST" else None
        form = admin_forms.UserPasswordForm(data, meta={"csrf_context": session})

        if request.method == "POST":
            if not form.validate():
                return aiohttp_jinja2.render_template(
                    "admin/user_password_modal.html",
                    request,
                    {"form": form, "target_user": user_obj},
                    status=400,
                )

            user_obj.password = password_context.hash(form.password.data)
            await db_session.commit()
            await admin_event("user_update", request)
            await flash(request, _("Password updated"), "success")
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response

        return aiohttp_jinja2.render_template(
            "admin/user_password_modal.html",
            request,
            {"form": form, "target_user": user_obj},
        )

    if action == "user_delete":
        target_user_id = int(item_id)
        is_htmx = request.headers.get("HX-Request", "").lower() == "true"

        if target_user_id == request["user"].id:
            message = _("You cannot delete yourself")
            if is_htmx:
                return web.Response(text=message, status=400)
            await flash(request, message, "error")
            raise web.HTTPFound(request.app.router["users"].url_for())

        total_users = await db_session.scalar(sa.select(sa.func.count(User.id))) or 0
        if total_users <= 1:
            message = _("Cannot delete the last user")
            if is_htmx:
                return web.Response(text=message, status=400)
            await flash(request, message, "error")
            raise web.HTTPFound(request.app.router["users"].url_for())

        user_obj = await db_session.scalar(
            sa.select(User).where(User.id == target_user_id)
        )
        if not user_obj:
            raise web.HTTPNotFound()

        await db_session.delete(user_obj)
        await db_session.commit()
        await admin_event("user_delete", request)

        if is_htmx:
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response

        await flash(request, _("User deleted"), "success")
        raise web.HTTPFound(request.app.router["users"].url_for())

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
                    "ai_settings_url": request.app.router["actions"].url_for(
                        action="update_ai_settings", item_id="global"
                    ),
                    "allow_ai_switch": True,
                },
            )
        return web.json_response({"ok": True, "provider": provider, "model": model})

    if action == "generate_topics":
        generate_project_topics.delay()
        await admin_event("topics_generate_request", request)
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
        document = await db_session.scalar(
            sa.select(Document).where(Document.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound(text="Document not found")
        await db_session.delete(document)
        await db_session.commit()
        response = web.Response(text="")
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    if action == "ignore_document":
        document = await db_session.scalar(
            sa.select(Document).where(Document.id == int(item_id))
        )
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

    if action == "delete_file":
        document = await db_session.scalar(
            sa.select(Document).where(Document.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound(text="Document not found")
        file_path = getattr(document, "uri", None)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        await db_session.delete(document)
        await db_session.commit()
        await admin_event("file_delete", request)
        return web.Response(text="", status=200)

    if action == "add_source":
        data = await request.post()
        session = await get_session(request)
        form = forms.SourceForm(data, meta={"csrf_context": session})
        if not form.validate():
            return web.Response(text="Error", status=400)

        uri = form.url.data
        parsed_uri = urlparse(uri)
        title = parsed_uri.netloc or parsed_uri.path
        reindex_cron = normalize_reindex_cron(form.reindex_cron.data)
        rule_types = data.getall("rule_type[]", [])
        rule_values = data.getall("rule_value[]", [])
        rules = [
            CrawlerRule(type=rt, value=rv.strip())
            for rt, rv in zip(rule_types, rule_values)
            if rv.strip()
        ]
        source = Source(
            uri=uri,
            title=title,
            config=SourceConfig(rules=rules),
            reindex_cron=reindex_cron,
        )
        db_session.add(source)
        await db_session.commit()
        await admin_event("source_create", request)
        crawl_source_task.delay(source.id)
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "delete_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        await db_session.delete(source)
        await db_session.commit()
        await admin_event("source_delete", request)
        return web.Response(text="", status=200)

    if action == "rebuild_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        await db_session.execute(
            sa.delete(Document).where(Document.source_id == source.id)
        )
        await db_session.commit()
        await admin_event("source_reindex_request", request)
        crawl_source_task.delay(source.id)
        return web.Response(text="ok")

    if action == "crawl_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        crawl_source_task.delay(source.id)
        await flash(request, _("Crawl task started for source"), "success")
        return web.Response(text="ok", status=200)

    if action == "refresh_source_index":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        refresh_source_index.delay(source.id)
        await admin_event("source_reindex_request", request)
        await flash(
            request,
            _("Update task started for %(title)s", title=source.title or source.uri),
            "success",
        )
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
        rows = (
            await db_session.execute(
                sa.select(Document.id).where(
                    Document.source_id.is_(None),
                    Document.uri.isnot(None),
                )
            )
        ).all()
        for row in rows:
            document_id = row[0] if isinstance(row, tuple) else row
            crawl_file_task.delay(document_id)
        await flash(request, _("Rebuild task started for uploaded files"), "success")
        return web.Response(text="ok", status=200)

    raise web.HTTPBadRequest(text="Unknown action")


@meta(title=_("Страницы"))
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
    source_filters = sorted(
        {
            (source.title or source.uri)
            for source in sources
            if (source.title or source.uri)
        },
        key=lambda value: value.lower(),
    )

    return {
        "project": _project_context(request),
        "sources": sources,
        "source_filters": source_filters,
    }


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

    size_bytes_expr = sa.cast(Document._length, sa.BigInteger).label("size_bytes")
    doc_type_expr = sa.func.nullif(Document.meta["doc_type"].astext, "").label(
        "document_type"
    )

    documents = (
        await request["db"].execute(
            sa.select(
                Document.id,
                Document.title,
                Document.uri,
                Document.created_at,
                Document.updated_at,
                Document.status,
                Document.is_ignored,
                Source.title.label("source_title"),
                Source.uri.label("source_uri"),
                size_bytes_expr,
                chunk_counts.c.chunk_count,
                doc_type_expr,
            )
            .join(Source, Document.source_id == Source.id)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
            .order_by(Document.created_at.desc())
        )
    ).all()

    data = []
    for (
        document_id,
        title,
        uri,
        created_at,
        updated_at,
        status,
        is_ignored,
        source_title,
        source_uri,
        size_bytes,
        chunk_count,
        document_type,
    ) in documents:
        doc_type_value = (
            document_type
            if isinstance(document_type, str) and document_type
            else DEFAULT_DOCUMENT_TYPE
        )
        data.append(
            {
                "id": str(document_id),
                "title": _display_document_title(title, uri),
                "source": source_title or source_uri or _("Файлы"),
                "created_at": created_at.isoformat() if created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
                "status": status,
                "is_ignored": is_ignored,
                "size_bytes": int(size_bytes or 0),
                "chunk_count": int(chunk_count or 0),
                "document_type": doc_type_value,
            }
        )

    return web.json_response(data)


@login_required()
async def project_files_json(request):
    rows = await _files_rows(request["db"])
    return web.json_response([_file_row_to_payload(row) for row in rows])


@meta(title=_("Stats"))
@login_required()
@aiohttp_jinja2.template("projects/stats.html")
async def project_stats(request):
    db = request["db"]

    tz_name = config.get("time_zone") or "UTC"
    app_tz = ZoneInfo(tz_name)
    now_local = datetime.now(app_tz)
    start_day_local = now_local.date() - timedelta(days=30)
    start_date_local = datetime.combine(start_day_local, time.min, tzinfo=app_tz)
    start_date_utc = start_date_local.astimezone(timezone.utc)

    chats_query = (
        sa.select(
            sa.func.date_trunc("day", sa.func.timezone(tz_name, Chat.created_at)).label(
                "day"
            ),
            sa.func.count(Chat.id).label("count"),
            sa.func.count(sa.distinct(Chat.user_uid)).label("users"),
        )
        .where(Chat.created_at >= start_date_utc)
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    chats_res = (await db.execute(chats_query)).all()

    msgs_query = (
        sa.select(
            sa.func.date_trunc(
                "day", sa.func.timezone(tz_name, ChatMsg.created_at)
            ).label("day"),
            sa.func.count(ChatMsg.id).label("count"),
            sa.func.sum(sa.func.jsonb_array_length(ChatMsg.used_chunks)).label("hits"),
            sa.func.sum(ChatMsg.tokens).label("tokens"),
        )
        .where(ChatMsg.created_at >= start_date_utc, ChatMsg.role == "assistant")
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    msgs_res = (await db.execute(msgs_query)).all()

    votes_query = (
        sa.select(
            sa.func.date_trunc(
                "day", sa.func.timezone(tz_name, ChatMsg.created_at)
            ).label("day"),
            sa.func.coalesce(
                sa.func.sum(sa.case((ChatMsg.vote.is_(True), 1), else_=0)),
                0,
            ).label("likes"),
            sa.func.coalesce(
                sa.func.sum(sa.case((ChatMsg.vote.is_(False), 1), else_=0)),
                0,
            ).label("dislikes"),
        )
        .where(ChatMsg.created_at >= start_date_utc, ChatMsg.role == "assistant")
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    votes_res = (await db.execute(votes_query)).all()

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
                "provider_label": provider_labels.get(
                    provider_key, provider_key.capitalize()
                ),
                "model": model_name,
                "model_label": model_labels.get((provider_key, model_name), model_name),
                "tokens": row.tokens or 0,
            }
        )
    token_breakdown.sort(key=lambda item: item["tokens"], reverse=True)

    stats = {}
    for i in range(31):
        d = (start_day_local + timedelta(days=i)).strftime("%Y-%m-%d")
        stats[d] = {
            "chats": 0,
            "users": 0,
            "messages": 0,
            "hits": 0,
            "tokens": 0,
            "likes": 0,
            "dislikes": 0,
        }

    for row in chats_res:
        d = row.day.strftime("%Y-%m-%d")
        if d not in stats:
            stats[d] = {
                "chats": 0,
                "users": 0,
                "messages": 0,
                "hits": 0,
                "tokens": 0,
                "likes": 0,
                "dislikes": 0,
            }
        stats[d]["chats"] = row.count
        stats[d]["users"] = row.users

    for row in msgs_res:
        d = row.day.strftime("%Y-%m-%d")
        if d not in stats:
            stats[d] = {
                "chats": 0,
                "users": 0,
                "messages": 0,
                "hits": 0,
                "tokens": 0,
                "likes": 0,
                "dislikes": 0,
            }
        stats[d]["messages"] = row.count
        stats[d]["hits"] = row.hits or 0
        stats[d]["tokens"] = row.tokens or 0

    for row in votes_res:
        d = row.day.strftime("%Y-%m-%d")
        if d not in stats:
            stats[d] = {
                "chats": 0,
                "users": 0,
                "messages": 0,
                "hits": 0,
                "tokens": 0,
                "likes": 0,
                "dislikes": 0,
            }
        stats[d]["likes"] = row.likes or 0
        stats[d]["dislikes"] = row.dislikes or 0

    labels = sorted(stats.keys())
    data_chats = [stats[d]["chats"] for d in labels]
    data_users = [stats[d]["users"] for d in labels]
    data_msgs = [stats[d]["messages"] for d in labels]
    data_hits = [stats[d]["hits"] for d in labels]
    data_tokens = [stats[d]["tokens"] for d in labels]
    data_likes = [stats[d]["likes"] for d in labels]
    data_dislikes = [stats[d]["dislikes"] for d in labels]

    total_unique_users = (
        await db.scalar(
            sa.select(sa.func.count(sa.distinct(Chat.user_uid))).where(
                Chat.created_at >= start_date_utc
            )
        )
        or 0
    )
    pending_embeddings = (
        await db.scalar(
            sa.select(sa.func.count(Chunk.id)).where(Chunk.embedding.is_(None))
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
            sa.func.coalesce(sa.func.sum(sa.func.length(Chunk.text)), 0).label(
                "chunk_storage"
            ),
        )
        .select_from(Source)
        .outerjoin(Document, Document.source_id == Source.id)
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .group_by(Source.id)
    )
    source_chunks_res = (await db.execute(source_chunks_query)).all()
    files_docs_row = (
        await db.execute(
            sa.select(
                sa.func.count(Document.id).label("doc_count"),
                sa.func.coalesce(sa.func.sum(Document._length), 0).label("data_volume"),
            ).where(Document.source_id.is_(None))
        )
    ).one()
    files_chunks_row = (
        await db.execute(
            sa.select(
                sa.func.count(Chunk.id).label("chunk_count"),
                sa.func.coalesce(sa.func.sum(sa.func.length(Chunk.text)), 0).label(
                    "chunk_storage"
                ),
            )
            .select_from(Document)
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.source_id.is_(None))
        )
    ).one()

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

    files_doc_count = int(files_docs_row.doc_count or 0)
    files_data_volume = int(files_docs_row.data_volume or 0)
    files_chunk_count = int(files_chunks_row.chunk_count or 0)
    files_chunk_storage = int(files_chunks_row.chunk_storage or 0)
    if files_doc_count > 0:
        source_stats.append(
            {
                "id": None,
                "title": _("Файлы"),
                "doc_count": files_doc_count,
                "data_volume": files_data_volume,
                "chunk_count": files_chunk_count,
                "chunk_storage": files_chunk_storage,
            }
        )
        total_docs += files_doc_count
        total_data_volume += files_data_volume
        total_chunks += files_chunk_count
        total_chunk_storage += files_chunk_storage

    return {
        "project": _project_context(request),
        "labels": labels,
        "data_chats": data_chats,
        "data_users": data_users,
        "data_msgs": data_msgs,
        "data_hits": data_hits,
        "data_tokens": data_tokens,
        "data_likes": data_likes,
        "data_dislikes": data_dislikes,
        "total_chats": sum(data_chats),
        "total_users": total_unique_users,
        "total_msgs": sum(data_msgs),
        "total_hits": sum(data_hits),
        "total_tokens": sum(data_tokens),
        "pending_embeddings": pending_embeddings,
        "token_breakdown": token_breakdown,
        "source_stats": source_stats,
        "total_docs": total_docs,
        "total_data_volume": total_data_volume,
        "total_chunks": total_chunks,
        "total_chunk_storage": total_chunk_storage,
    }


@login_required()
@aiohttp_jinja2.template("projects/document_content.html")
async def project_document_content(request):
    document_id = int(request.match_info.get("document_id"))
    return await _document_detail_context(request, document_id)


@meta(title=_("Структура документа"))
@login_required()
@aiohttp_jinja2.template("projects/document_detail.html")
async def project_document_detail(request):
    document_id = int(request.match_info.get("document_id"))
    return await _document_detail_context(request, document_id)


@meta(title=_("Chat"))
@login_required()
@aiohttp_jinja2.template("chat/chat.html")
async def project_chat(request):
    chat_id = (request.match_info.get("chat_id") or "").strip()
    if chat_id:
        chat = await request["db"].scalar(sa.select(Chat).where(Chat.id == chat_id))
        if not chat:
            raise web.HTTPNotFound(text="Chat not found")
        chat.meta = merge_chat_meta(chat.meta, request)
        await request["db"].commit()
    else:
        user_uid_param = request.rel_url.query.get("user_uid", "").strip()
        user_uid = user_uid_param or str(request["user"].id)

        project = _project_context(request)
        chat = Chat(
            title=f"Chat for {project.title}",
            user_uid=user_uid,
            meta=merge_chat_meta({}, request),
        )
        request["db"].add(chat)
        await request["db"].commit()
        await request["db"].refresh(chat)
        location = request.app.router["project_chat_with_id"].url_for(chat_id=chat.id)
        raise web.HTTPFound(location=location)

    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([request["user"].id, chat.id], salt="vchat")
    signed_chat_id = serializer.dumps(chat.id, salt="chat")
    history_rows = (
        (
            await request["db"].execute(
                sa.select(ChatMsg)
                .where(ChatMsg.chat_id == chat.id)
                .order_by(ChatMsg.created_at.asc(), ChatMsg.id.asc())
            )
        )
        .scalars()
        .all()
    )
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
                "sources": _message_sources(row),
            }
        )

    project = _project_context(request)
    provider_obj, model_obj = resolve_ai_settings(project.provider, project.model)
    ai_settings_url = request.app.router["actions"].url_for(
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
        "signed_chat_id": signed_chat_id,
    }


@meta(title=_("Integration"))
@login_required()
@aiohttp_jinja2.template("projects/integration.html")
async def project_integration(request):
    secret = get_setting(request.app, "project.secret", "") or ""
    if not secret:
        secret = secrets.token_urlsafe(32)
        await apply_settings_updates(
            request.app, request["db"], {"project.secret": secret}
        )
        await request["db"].commit()

    return {"project": _project_context(request), "project_secret": secret}


async def _render_public_chat(request):
    user_uid = request.query.get("user_uid", "").strip()
    user_name = request.query.get("user_name", "")
    user_email = request.query.get("user_email", "")
    sign = request.query.get("sign", "")

    if not user_uid:
        user_uid = f"guest_{uuid.uuid4().hex[:8]}"

    secret = get_setting(request.app, "project.secret", "") or ""
    if sign and secret:
        expected_sign = hmac.new(
            secret.encode("utf-8"), user_uid.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_sign, sign):
            return web.HTTPForbidden(text="Invalid signature")

    chat = Chat(
        title=f"Chat for {user_name or user_uid}",
        user_uid=user_uid,
        meta=merge_chat_meta(
            {"name": user_name, "email": user_email},
            request,
        ),
    )
    request["db"].add(chat)
    await request["db"].commit()
    await request["db"].refresh(chat)

    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([user_uid, chat.id], salt="vchat")
    signed_chat_id = serializer.dumps(chat.id, salt="chat")
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
        "signed_chat_id": signed_chat_id,
        "initial_messages": [],
    }


@meta(title=_("Chat Widget"))
@aiohttp_jinja2.template("chat/chat.html")
async def public_widget_chat(request):
    if not (request.app[CONFIG_KEY].get("vchat_chat") or "").strip():
        raise web.HTTPNotFound(text="Widget chat is not configured")
    return await _render_public_chat(request)


@meta(title=_("Files"))
@login_required()
@aiohttp_jinja2.template("projects/files.html")
async def project_files(request):
    db_session = request["db"]
    if request.method == "POST":
        await request.post()
        user = request["user"]
        author_name = user.name.strip()
        author_email = user.email.strip()
        if not author_name:
            author_name = author_email or f"user-{user.id}"

        content = ""
        document = Document(
            source_id=None,
            title=None,
            uri=None,
            content=content,
            hash_value=content,
            meta={
                "doc_type": "markdown",
                "content_type": "text/markdown",
                "author_id": user.id,
                "author_name": author_name,
                "author_email": author_email,
            },
            status="added",
        )
        document.length = len(content)
        db_session.add(document)
        await db_session.flush()
        # Default title is the document numeric ID.
        document.title = str(document.id)
        await db_session.commit()
        await admin_event("file_create", request)
        location = request.app.router["file_document"].url_for(
            document_id=str(document.id)
        )
        raise web.HTTPFound(location=location)

    files_rows = await _files_rows(db_session)

    return {
        "project": _project_context(request),
        "files_rows": files_rows,
        "current_document": None,
    }


@meta(title=_("Files"))
@login_required()
@aiohttp_jinja2.template("projects/files.html")
async def file_document(request):
    db_session = request["db"]
    document_id = int(request.match_info["document_id"])

    document = await db_session.scalar(
        sa.select(Document).where(
            Document.id == document_id,
            Document.source_id.is_(None),
            Document.uri.is_(None),
        )
    )
    if not document:
        raise web.HTTPNotFound()

    if request.method == "POST":
        data = await request.post()
        action = str(data.get("action") or "save")
        if action == "delete":
            await db_session.delete(document)
            await db_session.commit()
            await admin_event("file_delete", request)
            raise web.HTTPFound(location=request.app.router["project_files"].url_for())

        content = str(data.get("content") or "")
        document.content = content
        document.hash_value = content
        document.length = len(content)
        document.status = "added"
        document.updated_at = datetime.now(timezone.utc)
        await db_session.execute(
            sa.delete(Chunk).where(Chunk.document_id == document.id)
        )
        await db_session.commit()
        index_document.delay(document.id)
        await admin_event("file_update", request)
        await flash(request, _("Файл сохранен"), "success")
        raise web.HTTPFound(location=request.path)

    files_rows = await _files_rows(db_session)

    return {
        "project": _project_context(request),
        "files_rows": files_rows,
        "current_document": document,
    }


@login_required()
async def secure_download(request):
    file_id = int(request.match_info["file_id"])
    document = await request["db"].scalar(
        sa.select(Document).where(Document.id == file_id)
    )
    if not document or not getattr(document, "uri", None):
        raise web.HTTPNotFound(text="File not found")
    return web.FileResponse(path=document.uri)


async def on_upload(request, resource, file_path):
    user = request["user"]
    author_name = user.name.strip()
    author_email = user.email.strip()
    if not author_name:
        author_name = author_email or f"user-{user.id}"

    filename = getattr(resource, "file_name", None) or os.path.basename(str(file_path))
    document = Document(
        source_id=None,
        title=filename,
        uri=str(file_path),
        content="",
        hash_value="",
        meta={
            "filename": filename,
            "doc_type": Path(filename).suffix.lstrip(".").lower()
            or DEFAULT_DOCUMENT_TYPE,
            "author_id": user.id,
            "author_name": author_name,
            "author_email": author_email,
        },
        status="added",
    )
    document.length = 0
    request["db"].add(document)
    await request["db"].flush()
    await request["db"].commit()
    await admin_event("file_upload", request)
    crawl_file_task.delay(document.id)
    return document


@meta(title=_("Прогресс индексации"))
@login_required()
@aiohttp_jinja2.template("projects/progress.html")
async def project_progress(request):
    db_session = request["db"]
    r = request.app[REDIS_KEY]

    # ---- Статистика по документам из БД ----
    chunk_counts_sq = (
        sa.select(
            Chunk.document_id,
            sa.func.count(Chunk.id).label("chunk_count"),
        )
        .where(Chunk.document_id.isnot(None))
        .group_by(Chunk.document_id)
        .subquery()
    )

    overall_row = (
        await db_session.execute(
            sa.select(
                sa.func.count(Document.id).label("total"),
                sa.func.count(Document.id)
                .filter(sa.func.coalesce(chunk_counts_sq.c.chunk_count, 0) > 0)
                .label("done"),
            )
            .outerjoin(chunk_counts_sq, chunk_counts_sq.c.document_id == Document.id)
            .where(Document.is_ignored.is_(False))
            .where(Document.source_id.isnot(None))
        )
    ).one()

    total_docs = overall_row.total or 0
    done_docs = overall_row.done or 0
    remaining_docs = total_docs - done_docs
    pct = round(done_docs / total_docs * 100) if total_docs else 0

    # Статистика по источникам
    source_rows = (
        await db_session.execute(
            sa.select(
                Source.id,
                Source.title,
                sa.func.count(Document.id).label("total"),
                sa.func.count(Document.id)
                .filter(sa.func.coalesce(chunk_counts_sq.c.chunk_count, 0) > 0)
                .label("done"),
            )
            .outerjoin(
                Document,
                sa.and_(
                    Document.source_id == Source.id,
                    Document.is_ignored.is_(False),
                ),
            )
            .outerjoin(chunk_counts_sq, chunk_counts_sq.c.document_id == Document.id)
            .group_by(Source.id, Source.title)
            .order_by(Source.id)
        )
    ).all()

    sources = []
    for row in source_rows:
        src_total = row.total or 0
        src_done = row.done or 0
        src_remaining = src_total - src_done
        src_pct = round(src_done / src_total * 100) if src_total else 0
        sources.append(
            {
                "id": row.id,
                "title": row.title,
                "total": src_total,
                "done": src_done,
                "remaining": src_remaining,
                "pct": src_pct,
            }
        )

    # ---- Среднее время обработки из Redis ----
    raw_times = await r.lrange(EMBED_STATS_KEY, 0, -1)
    durations = []
    for v in raw_times:
        try:
            durations.append(float(v))
        except (ValueError, TypeError):
            pass

    avg_chunk_sec = sum(durations) / len(durations) if durations else None

    avg_chunks_row = (
        await db_session.execute(
            sa.select(sa.func.avg(chunk_counts_sq.c.chunk_count)).select_from(
                chunk_counts_sq
            )
        )
    ).scalar()
    avg_chunks_per_doc = float(avg_chunks_row) if avg_chunks_row else None

    avg_doc_sec = None
    eta_sec = None
    if avg_chunk_sec is not None and avg_chunks_per_doc:
        avg_doc_sec = avg_chunk_sec * avg_chunks_per_doc
        eta_sec = avg_doc_sec * remaining_docs

    def fmt_duration(sec):
        if sec is None:
            return None
        sec = int(sec)
        if sec < 60:
            return f"{sec}с"
        if sec < 3600:
            return f"{sec // 60}м {sec % 60}с"
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h}ч {m}м"

    return {
        "project": _project_context(request),
        "total_docs": total_docs,
        "done_docs": done_docs,
        "remaining_docs": remaining_docs,
        "pct": pct,
        "sources": sources,
        "avg_doc_sec": round(avg_doc_sec, 1) if avg_doc_sec else None,
        "avg_doc_fmt": fmt_duration(avg_doc_sec),
        "eta_fmt": fmt_duration(eta_sec),
        "samples": len(durations),
    }
