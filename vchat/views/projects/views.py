import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
from aiohttp_tus.utils import parse_upload_metadata
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer
from sqlalchemy.orm.attributes import flag_modified

from vchat.ai_providers import (
    DEFAULT_OPENAI_MODEL,
    get_ai_provider_options,
    get_default_model_id,
    get_default_provider_id,
    is_model_available,
    is_provider_available,
    resolve_ai_settings,
)
from jobs.crawler import (
    crawl_all_sources_task,
    crawl_file_task,
    crawl_source_task,
)
from jobs.embedder.tasks import (
    index_project,
    refresh_project_index,
    refresh_source_index,
)
from jobs.suggestions import generate_project_topics
from vchat.app_keys import SIGNER_KEY
from vchat.document_types import DEFAULT_DOCUMENT_TYPE
from vchat.i18n import lazy_gettext as _
from vchat.models import (
    Chat,
    ChatMsg,
    Chunk,
    Document,
    Project,
    Source,
    User,
)
from vchat.settings import config
from vchat.utils import flash, login_required, meta

from . import forms

logger = logging.getLogger(__name__)

__all__ = [
    "index",
    "project_onboarding",
    "project_edit",
    "project_action",
    "project_edit_sources",
    "project_source_edit",
    "project_edit_users",
    "project_view",
    "project_document_content",
    "project_documents_json",
    "project_chat",
    "project_call",
    "project_stats",
    "project_topics",
    "project_integration",
    "public_project_chat",
    "project_files",
    "secure_download",
    "delete_file",
    "on_upload",
]


def _default_project_title(user) -> str:
    user_name = getattr(user, "name", "") or _("My")
    return _("{}'s project").format(user_name)


@meta(title=_("Project List"))
@login_required()
@aiohttp_jinja2.template("projects/index.html")
async def index(request):
    result = await request["db"].execute(
        sa.select(Project)
        .join(ProjectUser)
        .where(ProjectUser.user_id == request["user"].id)
    )
    projects = result.scalars().all()
    return {"projects": projects}


@meta(title=_("Create your first project"))
@login_required()
@aiohttp_jinja2.template("auth/onboarding.html")
async def project_onboarding(request):
    db_session = request["db"]
    session = await get_session(request)
    user_id = request["user"].id

    has_projects = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectUser)
        .where(ProjectUser.user_id == user_id)
    )

    if has_projects:
        raise web.HTTPFound(request.app.router["project_list"].url_for())

    data = await request.post() if request.method == "POST" else None
    form_kwargs = {"meta": {"csrf_context": session}}
    default_project_title = _default_project_title(request["user"])
    if data:
        form_kwargs["formdata"] = data
    else:
        form_kwargs["data"] = {"project_title": default_project_title}
    form = forms.OnboardingForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        project = Project(
            title=form.project_title.data,
            user_id=user_id,
            config={},
            system_prompt=form.system_prompt.data or forms.DEFAULT_SYSTEM_PROMPT,
            agent_style=None,
            provider=get_default_provider_id(),
            model=get_default_model_id(),
        )
        db_session.add(project)
        await db_session.flush()

        project_user = ProjectUser(
            project_id=project.id,
            user_id=user_id,
            role="owner",
        )
        db_session.add(project_user)

        source_title = form.source_title.data
        if not source_title:
            parsed = urlparse(form.source_url.data)
            source_title = parsed.netloc or parsed.path or form.source_url.data

        source = Source(
            project_id=project.id,
            type="site",
            uri=form.source_url.data,
            title=source_title,
            config={},
        )
        db_session.add(source)
        crawl_source_task.delay(source.id)
        await db_session.commit()
        await db_session.refresh(project)
        await flash(request, _("Project created"), "success")
        raise web.HTTPFound(
            request.app.router["project_view"].url_for(project_id=str(project.short_id))
        )

    return {"form": form}


@meta(title=_("Project Add"))
@login_required()
@aiohttp_jinja2.template("projects/edit.html")
async def project_edit(request):
    db_session = request["db"]
    session = await get_session(request)
    data = await request.post()
    project_id = request.match_info.get("project_id", "new")
    is_new = project_id == "new"

    if not is_new:
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == request["user"].id,
            )
        )
        if not project:
            raise web.HTTPNotFound()
    else:
        project = Project(id="new")
        project.title = _default_project_title(request["user"])
        project.system_prompt = forms.DEFAULT_SYSTEM_PROMPT
        project.provider = get_default_provider_id()
        project.model = get_default_model_id(project.provider)

    form_kwargs = {"meta": {"csrf_context": session}, "obj": project}
    if data:
        form_kwargs["formdata"] = data
    form = forms.ProjectForm(**form_kwargs)

    if not data:
        existing_config = getattr(project, "config", None) or {}
        form.agent_name.data = existing_config.get("agent_name", "")
        form.welcome_message.data = existing_config.get("welcome_message", "")

    if request.method == "POST" and form.validate():
        if is_new:
            project = Project(
                title=form.title.data,
                user_id=request["user"].id,
                config={},
                system_prompt=form.system_prompt.data,
                agent_style=form.agent_style.data,
                provider=form.provider.data,
                model=form.model.data,
            )
            db_session.add(project)
            await db_session.flush()  # Ensure project.id is available

            project_user = ProjectUser(
                project_id=project.id,
                user_id=request["user"].id,
                role="owner",
            )
            db_session.add(project_user)
            message = _("Project created")
        else:
            project.title = form.title.data
            project.system_prompt = form.system_prompt.data
            project.agent_style = form.agent_style.data
            project.provider = form.provider.data
            project.model = form.model.data
            message = _("Project updated")
        agent_name = (form.agent_name.data or "").strip()
        welcome_message = (form.welcome_message.data or "").strip()
        project_config = getattr(project, "config", None) or {}
        project_config["agent_name"] = agent_name
        project_config["welcome_message"] = welcome_message
        project.config = project_config
        flag_modified(project, "config")

        await db_session.commit()
        await flash(request, message, "success")
        raise web.HTTPFound(request.app.router["project_list"].url_for())

    is_owner = False
    if not is_new:
        project_user_role = await db_session.scalar(
            sa.select(ProjectUser.role).where(
                ProjectUser.project_id == project.id,
                ProjectUser.user_id == request["user"].id,
            )
        )
        is_owner = project_user_role == "owner"
    else:
        is_owner = True  # Creator is owner

    return {
        "form": form,
        "project": project,
        "is_owner": is_owner,
        "ai_provider_options": get_ai_provider_options(),
    }


@meta(title=_("Project Edit Sources"))
@login_required()
@aiohttp_jinja2.template("projects/sources.html")
async def project_edit_sources(request):
    db_session = request["db"]
    project_id = request.match_info.get("project_id")
    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Fetch sources with document counts
    stmt = (
        sa.select(Source, sa.func.count(Document.id).label("doc_count"))
        .outerjoin(Document, Document.source_id == Source.id)
        .where(Source.project_id == project.id)
        .group_by(Source.id)
        .order_by(Source.created_at.desc())
    )
    sources = (await db_session.execute(stmt)).all()

    session = await get_session(request)
    form = forms.SourceForm(meta={"csrf_context": session})
    return {"project": project, "sources": sources, "form": form}


@meta(title=_("Edit Source"))
@login_required()
@aiohttp_jinja2.template("projects/source_edit.html")
async def project_source_edit(request):
    project_id = request.match_info.get("project_id")
    source_id = request.match_info.get("source_id")

    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    source = await request["db"].scalar(
        sa.select(Source).where(
            Source.short_id == source_id, Source.project_id == project.id
        )
    )
    if not source:
        raise web.HTTPNotFound()

    session = await get_session(request)
    data = await request.post()

    # Populate form with source data if not POST
    form_kwargs = {"meta": {"csrf_context": session}, "obj": source}
    if data:
        form_kwargs["formdata"] = data
    else:
        form_data = {
            "type": source.type,
            "title": source.title,
        }

        # Load S3 credentials from config if S3 source
        if source.type == "s3":
            config = source.config or {}
            form_data.update(
                {
                    "aws_access_key_id": config.get("aws_access_key_id", ""),
                    "aws_secret_access_key": config.get("aws_secret_access_key", ""),
                    "bucket_name": config.get("bucket_name", ""),
                    "endpoint_url": config.get(
                        "endpoint_url", "https://s3.amazonaws.com"
                    ),
                    "region": config.get("region", "us-east-1"),
                    "prefix": config.get("prefix", ""),
                }
            )
        elif source.type == "google_drive":
            config = source.config or {}
            form_data.update(
                {
                    "google_drive_folder_id": config.get("folder_id", ""),
                    "google_drive_folder_name": config.get("folder_name", ""),
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

        if source_type == "s3":
            # Update S3 credentials in config
            source.config = {
                "aws_access_key_id": form.aws_access_key_id.data,
                "aws_secret_access_key": form.aws_secret_access_key.data,
                "bucket_name": form.bucket_name.data,
                "endpoint_url": form.endpoint_url.data or "",
                "region": form.region.data or "us-east-1",
                "prefix": form.prefix.data or "",
            }
            source.uri = f"s3://{form.bucket_name.data}"
        elif source_type == "google_drive":
            source.config = {
                "folder_id": form.google_drive_folder_id.data,
                "folder_name": form.google_drive_folder_name.data,
            }
            # Preserve refresh token if it exists in old config
            if source.config and "refresh_token" in source.config:
                source.config["refresh_token"] = source.config["refresh_token"]

            # Update with new refresh token if in session
            session = await get_session(request)
            if session.get("google_refresh_token"):
                source.config["refresh_token"] = session.get("google_refresh_token")

            source.uri = f"gdrive://{form.google_drive_folder_id.data}"
        else:
            source.uri = form.url.data

            # Extract rules
            rule_types = data.getall("rule_type[]", [])
            rule_values = data.getall("rule_value[]", [])
            rules = []
            for r_type, r_value in zip(rule_types, rule_values):
                if r_value.strip():
                    rules.append({"type": r_type, "value": r_value.strip()})

            # Preserve existing config if any, but update rules
            new_config = source.config.copy() if source.config else {}
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

    return {"project": project, "source": source, "form": form}


@meta(title=_("Project Edit Users"))
@login_required()
@aiohttp_jinja2.template("projects/users.html")
async def project_edit_users(request):
    project_id = request.match_info.get("project_id")
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    current_project_user = await request["db"].scalar(
        sa.select(ProjectUser).where(
            ProjectUser.project_id == project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    current_user_role = current_project_user.role if current_project_user else None

    query = (
        sa.select(User, ProjectUser)
        .join(ProjectUser, ProjectUser.user_id == User.id)
        .where(ProjectUser.project_id == project.id)
    )

    q = request.query.get("q")
    if q:
        query = query.where(User.email.ilike(f"%{q}%"))

    users = (await request["db"].execute(query)).all()

    session = await get_session(request)
    form = forms.InviteUserForm(meta={"csrf_context": session})

    return {
        "project": project,
        "users": users,
        "form": form,
        "current_user_role": current_user_role,
        "q": q,
    }


@meta(title=_("Project Topics"))
@login_required()
@aiohttp_jinja2.template("projects/topics.html")
async def project_topics(request):
    db_session = request["db"]
    project_id = request.match_info.get("project_id")
    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    session = await get_session(request)
    current_meta = project.meta or {}

    # Pre-fill form from meta
    form_data = None
    if request.method == "GET":
        form_data = {
            "topics": "\n".join(current_meta.get("topics", [])),
            "intents": "\n".join(current_meta.get("intents", [])),
        }

    form_kwargs = {"meta": {"csrf_context": session}}
    if request.method == "POST":
        data = await request.post()
        form_kwargs["formdata"] = data
    elif form_data:
        form_kwargs["data"] = form_data

    form = forms.TopicsForm(**form_kwargs)

    if request.method == "POST" and form.validate():
        # Always save on POST
        new_meta = current_meta.copy()

        topics_list = [t.strip() for t in form.topics.data.split("\n") if t.strip()]
        intents_list = [i.strip() for i in form.intents.data.split("\n") if i.strip()]

        new_meta["topics"] = topics_list
        new_meta["intents"] = intents_list

        project.meta = new_meta
        flag_modified(project, "meta")
        await db_session.commit()

        await flash(request, _("Topics updated"), "success")
        raise web.HTTPFound(request.path)

    return {"project": project, "form": form}


@login_required()
async def project_action(request):
    db_session = request["db"]
    item_id = request.match_info.get("item_id")
    action = request.match_info.get("action")
    user_id = request["user"].id
    ok = "ok"

    # CSRF Check
    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    try:
        signed_user_id = request.app[SIGNER_KEY].loads(token, max_age=86400)
        if signed_user_id != user_id:
            raise web.HTTPForbidden(text="Invalid CSRF Token Owner")
    except (BadSignature, SignatureExpired):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    if action == "create_project":
        data = await request.post()
        title = data.get("title")
        if not title:
            return web.HTTPBadRequest(text="Title required")

        project = Project(
            title=title,
            user_id=user_id,
            config={},
            description=data.get("description"),
            system_prompt=data.get("system_prompt") or forms.DEFAULT_SYSTEM_PROMPT,
            agent_style=data.get("agent_style"),
            provider=get_default_provider_id(),
            model=get_default_model_id(),
        )
        db_session.add(project)
        await db_session.flush()

        project_user = ProjectUser(
            project_id=project.id,
            user_id=user_id,
            role="owner",
        )
        db_session.add(project_user)
        await db_session.commit()

        await flash(request, _("Project created"), "success")
        response = web.Response(text="ok")
        response.headers["HX-Redirect"] = str(
            request.app.router["project_edit_sources"].url_for(
                project_id=str(project.id)
            )
        )
        return response

    elif action == "update_ai_settings":
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == item_id,
                ProjectUser.user_id == user_id,
            )
        )
        if not project:
            raise web.HTTPNotFound(text="Project not found")

        data = await request.post()
        provider = (data.get("provider") or "").strip()
        model = (data.get("model") or "").strip()

        if not provider or not is_provider_available(provider):
            raise web.HTTPBadRequest(text="Unknown provider")

        if not model or not is_model_available(provider, model):
            model = get_default_model_id(provider)

        project.provider = provider
        project.model = model
        await db_session.commit()

        if request.headers.get("HX-Request"):
            provider_obj, model_obj = resolve_ai_settings(
                project.provider,
                project.model,
            )
            return aiohttp_jinja2.render_template(
                "chat/includes/ai_settings.html",
                request,
                {
                    "project": project,
                    "ai_provider_options": get_ai_provider_options(),
                    "current_ai_provider": provider_obj.id,
                    "current_ai_model": model_obj.id,
                    "ai_settings_url": request.app.router["project_actions"].url_for(
                        action="update_ai_settings", item_id=project.short_id
                    ),
                    "allow_ai_switch": True,
                },
            )

        return web.json_response({"ok": True, "provider": provider, "model": model})

    elif action == "generate_topics":
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == item_id,
                ProjectUser.user_id == user_id,
            )
        )
        if not project:
            raise web.HTTPNotFound(text="Project not found")

        generate_project_topics.delay(project.id)
        await flash(request, _("Topics generation started in background"), "success")
        return web.json_response({"ok": True})

    elif action == "delete_project":
        project_id = item_id

        result = await db_session.execute(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == user_id,
                ProjectUser.role == "owner",
            )
        )
        project = result.scalars().unique().one_or_none()
        if not project:
            raise web.HTTPNotFound(text="Project not found")

        await db_session.execute(
            sa.delete(ProjectUser).where(ProjectUser.project_id == project_id)
        )
        await db_session.delete(project)
        await db_session.commit()
        await flash(request, _("Project deleted"), "success")

        # Find next available project
        next_project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(ProjectUser.user_id == user_id)
            .limit(1)
        )

        response = web.Response(text=ok, status=200)
        if next_project:
            response.headers["HX-Redirect"] = str(
                request.app.router["project_view"].url_for(
                    project_id=str(next_project.id)
                )
            )
        else:
            response.headers["HX-Redirect"] = str(
                request.app.router["project_list"].url_for()
            )
        return response

    elif action == "delete_document":
        result = await db_session.execute(
            sa.select(Document)
            .join(Source, Document.source_id == Source.id)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(
                Document.short_id == item_id,
                ProjectUser.user_id == user_id,
            )
        )
        document = result.scalars().first()

        if not document:
            raise web.HTTPNotFound(text="Document not found or access denied")

        await db_session.delete(document)
        await db_session.commit()

        response = web.Response(text="")
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    elif action == "ignore_document":
        # Verify project access
        result = await db_session.execute(
            sa.select(Document)
            .join(Source, Document.source_id == Source.id)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(
                Document.short_id == item_id,
                ProjectUser.user_id == user_id,
            )
        )
        document = result.scalars().first()

        if not document:
            raise web.HTTPNotFound(text="Document not found or access denied")

        data = await request.post()
        raw_value = data.get("is_ignored")
        if raw_value is not None:
            should_ignore = str(raw_value).lower() in {"1", "true", "yes", "on"}
        else:
            should_ignore = not bool(document.is_ignored)

        document.is_ignored = should_ignore
        await db_session.commit()

        response = web.json_response({"is_ignored": document.is_ignored})
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    elif action == "add_source":
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == item_id,
                ProjectUser.user_id == user_id,
            )
        )
        if not project:
            raise web.HTTPNotFound(text="Project not found or access denied")

        data = await request.post()
        session = await get_session(request)
        form = forms.SourceForm(data, meta={"csrf_context": session})

        if form.validate():
            title = form.title.data
            source_type = form.type.data
            config = {}
            uri = ""

            if source_type == "s3":
                # Store S3 credentials in config
                config = {
                    "aws_access_key_id": form.aws_access_key_id.data,
                    "aws_secret_access_key": form.aws_secret_access_key.data,
                    "bucket_name": form.bucket_name.data,
                    "endpoint_url": form.endpoint_url.data or "",
                    "region": form.region.data or "us-east-1",
                    "prefix": form.prefix.data or "",
                }
                # For S3, use bucket name as URI
                uri = f"s3://{form.bucket_name.data}"
                if not title:
                    title = form.bucket_name.data
            elif source_type == "google_drive":
                config = {
                    "folder_id": form.google_drive_folder_id.data,
                    "folder_name": form.google_drive_folder_name.data,
                }
                if session.get("google_refresh_token"):
                    config["refresh_token"] = session.get("google_refresh_token")

                uri = f"gdrive://{form.google_drive_folder_id.data}"
                if not title:
                    title = form.google_drive_folder_name.data or "Google Drive"
            else:
                # For site/sitemap/list, use URL
                uri = form.url.data
                if not title:
                    from urllib.parse import urlparse

                    parsed_uri = urlparse(form.url.data)
                    title = parsed_uri.netloc or parsed_uri.path

                # Extract rules
                rule_types = data.getall("rule_type[]", [])
                rule_values = data.getall("rule_value[]", [])
                rules = []
                for r_type, r_value in zip(rule_types, rule_values):
                    if r_value.strip():
                        rules.append({"type": r_type, "value": r_value.strip()})

                if rules:
                    config["rules"] = rules

            source = Source(
                project_id=project.id,
                type=source_type,
                uri=uri,
                title=title,
                config=config,
            )
            db_session.add(source)
            await db_session.commit()

            # Trigger crawl after commit
            crawl_source_task.delay(source.id)
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response
        else:
            return web.Response(text="Error", status=400)

    elif action == "delete_source":
        source_id = item_id
        # Verify ownership
        source = await db_session.scalar(
            sa.select(Source)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(Source.short_id == source_id, ProjectUser.user_id == user_id)
        )
        if source:
            await db_session.delete(source)
            await db_session.commit()
            return web.Response(text="", status=200)  # Empty response to remove element
        return web.HTTPNotFound()

    elif action == "rebuild_source":
        source_id = item_id
        # Verify ownership
        source = await db_session.scalar(
            sa.select(Source)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(Source.short_id == source_id, ProjectUser.user_id == user_id)
        )
        if source:
            # Delete all documents for this source
            # Chunks will be deleted via cascade
            await db_session.execute(
                sa.delete(Document).where(Document.source_id == source.id)
            )
            await db_session.commit()

            # Trigger crawl
            crawl_source_task.delay(source.id)

            return web.Response(text="ok")
        return web.HTTPNotFound()

    elif action == "invite_user":
        project_id = item_id
        data = await request.post()
        session = await get_session(request)
        form = forms.InviteUserForm(data, meta={"csrf_context": session})

        if form.validate():
            email = form.email.data
            user = await db_session.scalar(sa.select(User).where(User.email == email))
            if user:
                # Check if already in project
                # Need to resolve project_id to int first
                project = await db_session.scalar(
                    sa.select(Project).where(Project.short_id == project_id)
                )
                if not project:
                    return web.HTTPNotFound(text="Project not found")

                exists = await db_session.scalar(
                    sa.select(ProjectUser).where(
                        ProjectUser.project_id == project.id,
                        ProjectUser.user_id == user.id,
                    )
                )
                if not exists:
                    project_user = ProjectUser(
                        project_id=project.id,
                        user_id=user.id,
                        role="member",  # Default role
                    )
                    db_session.add(project_user)
                    await db_session.commit()
                    await flash(request, _("User invited"), "success")
                else:
                    await flash(request, _("User already in project"), "warning")
            else:
                await flash(request, _("User not found"), "error")

            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response
        return web.Response(text="Error", status=400)

    elif action == "edit_user":
        project_user_id = int(item_id)
        data = await request.post()
        role = data.get("role")

        if role not in ["owner", "member"]:
            return web.HTTPBadRequest(text="Invalid role")

        project_user = await db_session.scalar(
            sa.select(ProjectUser)
            .join(Project, ProjectUser.project_id == Project.id)
            .where(ProjectUser.id == project_user_id)
        )

        if project_user:
            # Check if current user is owner of the project
            current_user_is_owner = await db_session.scalar(
                sa.select(ProjectUser).where(
                    ProjectUser.project_id == project_user.project_id,
                    ProjectUser.user_id == user_id,
                    ProjectUser.role == "owner",
                )
            )

            if not current_user_is_owner:
                return web.HTTPForbidden(text="Only owners can edit users")

            if role == "member" and project_user.role == "owner":
                # Check if there are other owners
                other_owners_count = await db_session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ProjectUser)
                    .where(
                        ProjectUser.project_id == project_user.project_id,
                        ProjectUser.role == "owner",
                        ProjectUser.id != project_user.id,
                    )
                )
                if other_owners_count == 0:
                    await flash(
                        request, _("Cannot change role of the last owner"), "error"
                    )
                    response = web.Response(text="ok")
                    response.headers["HX-Refresh"] = "true"
                    return response

            project_user.role = role
            await db_session.commit()
            await flash(request, _("User role updated"), "success")
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response

        return web.HTTPNotFound()

    elif action == "delete_user":
        project_user_id = int(item_id)
        project_user = await db_session.scalar(
            sa.select(ProjectUser)
            .join(Project, ProjectUser.project_id == Project.id)
            .join(
                ProjectUser.alias("owner"), Project.id == sa.text("owner.project_id")
            )  # Check if requester is owner?
            .where(ProjectUser.id == project_user_id)
        )
        if project_user:
            # Check if current user is in the same project
            current_user_in_project = await db_session.scalar(
                sa.select(ProjectUser).where(
                    ProjectUser.project_id == project_user.project_id,
                    ProjectUser.user_id == user_id,
                )
            )
            if current_user_in_project:
                # Check if the user to be deleted is an owner
                if project_user.role == "owner":
                    # Check if there are other owners
                    other_owners_count = await db_session.scalar(
                        sa.select(sa.func.count())
                        .select_from(ProjectUser)
                        .where(
                            ProjectUser.project_id == project_user.project_id,
                            ProjectUser.role == "owner",
                            ProjectUser.id != project_user.id,
                        )
                    )
                    if other_owners_count == 0:
                        await flash(request, _("Cannot delete the last owner"), "error")
                        response = web.Response(text="ok")
                        response.headers["HX-Refresh"] = "true"
                        return response

                await db_session.delete(project_user)
                await db_session.commit()
                return web.Response(text="", status=200)

        return web.HTTPNotFound()

    elif action == "crawl_source":
        source_id = item_id
        # Verify ownership
        source = await db_session.scalar(
            sa.select(Source)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(Source.short_id == source_id, ProjectUser.user_id == user_id)
        )
        if source:
            # Trigger the Celery task
            crawl_source_task.delay(source.id)
            await flash(request, _("Crawl task started for source"), "success")
            return web.Response(text="ok", status=200)
        return web.HTTPNotFound()

    elif action == "refresh_source_index":
        source_id = item_id
        source = await db_session.scalar(
            sa.select(Source)
            .join(Project, Source.project_id == Project.id)
            .join(ProjectUser, Project.id == ProjectUser.project_id)
            .where(Source.short_id == source_id, ProjectUser.user_id == user_id)
        )
        if source:
            refresh_source_index.delay(source.id)
            await flash(
                request,
                _(
                    "Update task started for %(title)s",
                    title=source.title or source.uri,
                ),
                "success",
            )
            return web.Response(text="ok", status=200)
        return web.HTTPNotFound()

    elif action == "crawl_all":
        project_id = item_id
        # Verify ownership
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == user_id,
            )
        )
        if project:
            # Trigger the Celery task
            crawl_all_sources_task.delay(project.id)
            await flash(request, _("Crawl task started for all sources"), "success")
            return web.Response(text="ok", status=200)
        return web.HTTPNotFound()

    elif action == "refresh_project_index":
        project_id = item_id
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == user_id,
            )
        )
        if project:
            refresh_project_index.delay(project.id)
            await flash(
                request,
                _("Update task started for project"),
                "success",
            )
            return web.Response(text="ok", status=200)
        return web.HTTPNotFound()

    elif action == "index_project":
        project_id = item_id
        # Verify ownership
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == user_id,
            )
        )
        if project:
            # Trigger the Celery task
            index_project.delay(project.id)
            await flash(
                request,
                _("Full rebuild task started for project"),
                "success",
            )
            return web.Response(text="ok", status=200)
        return web.HTTPNotFound()

    elif action == "reset_secret":
        project_id = item_id
        project = await db_session.scalar(
            sa.select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                Project.short_id == project_id,
                ProjectUser.user_id == user_id,
            )
        )
        if not project:
            raise web.HTTPNotFound()

        project_config = project.config or {}
        project_config["secret"] = secrets.token_urlsafe(32)
        project.config = project_config
        flag_modified(project, "config")
        await db_session.commit()

        return aiohttp_jinja2.render_template(
            "projects/_integration_secret_field.html",
            request,
            {"project": project, "project_secret": project_config["secret"]},
        )

    raise web.HTTPBadRequest(text="Unknown action")


@meta(title=_("Project Data"))
@login_required()
@aiohttp_jinja2.template("projects/view.html")
async def project_view(request):
    project_id = request.match_info.get("project_id")
    db_session = request["db"]
    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    sources = (
        (
            await db_session.execute(
                sa.select(Source)
                .where(Source.project_id == project.id)
                .order_by(Source.title.asc(), Source.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    return {"project": project, "sources": sources}


@login_required()
async def project_documents_json(request):
    project_id = request.match_info.get("project_id")
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

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

    # Get all documents with their sources and metadata
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
            .where(Source.project_id == project.id)
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
        size_value = int(size_bytes or 0)
        chunk_value = int(chunk_count or 0)
        data.append(
            {
                "id": doc.short_id,
                "title": doc.title
                or (doc.uri.split("/")[-1] if doc.uri else "Untitled"),
                "source": source.title or source.uri,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
                "status": doc.status,
                "is_ignored": doc.is_ignored,
                "uri": doc.uri,
                "size_bytes": size_value,
                "chunk_count": chunk_value,
                "document_type": doc_type_value,
                "meta": meta_payload,
            }
        )

    return web.json_response(data)


@meta(title=_("Project Stats"))
@login_required()
@aiohttp_jinja2.template("projects/stats.html")
async def project_stats(request):
    project_id = request.match_info.get("project_id")
    db = request["db"]

    project = await db.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Calculate stats for the last 30 days
    # We need:
    # - Chats per day
    # - Unique users per day
    # - Messages per day
    # - Document hits per day (from used_chunks)

    # Use a raw SQL query for aggregation for efficiency or use SQLAlchemy
    # Let's use SQLAlchemy

    from datetime import datetime, timedelta

    start_date = datetime.utcnow() - timedelta(days=30)

    # 1. Chats per day
    chats_query = (
        sa.select(
            sa.func.date_trunc("day", Chat.created_at).label("day"),
            sa.func.count(Chat.id).label("count"),
            sa.func.count(sa.distinct(Chat.user_uid)).label("users"),
        )
        .where(Chat.project_id == project.id, Chat.created_at >= start_date)
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )
    chats_res = (await db.execute(chats_query)).all()

    # 2. Messages and Hits per day
    # We need to join ChatMsg with Chat to filter by project_id
    msgs_query = (
        sa.select(
            sa.func.date_trunc("day", ChatMsg.created_at).label("day"),
            sa.func.count(ChatMsg.id).label("count"),
            sa.func.sum(sa.func.jsonb_array_length(ChatMsg.used_chunks)).label("hits"),
            sa.func.sum(ChatMsg.tokens).label("tokens"),
        )
        .join(Chat, ChatMsg.chat_id == Chat.id)
        .where(
            Chat.project_id == project.id,
            ChatMsg.created_at >= start_date,
            ChatMsg.role == "assistant",
        )
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
        .join(Chat, ChatMsg.chat_id == Chat.id)
        .where(Chat.project_id == project.id, ChatMsg.role == "assistant")
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
        provider_key = row.provider or get_default_provider_id()
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

    # Process results into a dictionary keyed by date string
    stats = {}

    # Initialize with 0s for the last 30 days
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
            # hits can be None if sum returns null
            stats[d]["hits"] = row.hits or 0
            stats[d]["tokens"] = row.tokens or 0

    # Convert to list for frontend
    labels = sorted(stats.keys())
    data_chats = [stats[d]["chats"] for d in labels]
    data_users = [stats[d]["users"] for d in labels]
    data_msgs = [stats[d]["messages"] for d in labels]
    data_hits = [stats[d]["hits"] for d in labels]
    data_tokens = [stats[d]["tokens"] for d in labels]

    # Totals
    total_chats = sum(data_chats)
    # For total unique users in period:
    total_unique_users = (
        await db.scalar(
            sa.select(sa.func.count(sa.distinct(Chat.user_uid))).where(
                Chat.project_id == project.id, Chat.created_at >= start_date
            )
        )
        or 0
    )

    total_msgs = sum(data_msgs)
    total_hits = sum(data_hits)
    total_tokens = sum(data_tokens)

    # Source data statistics
    # Query documents grouped by source
    source_docs_query = (
        sa.select(
            Source.id,
            Source.title,
            sa.func.count(Document.id).label("doc_count"),
            sa.func.coalesce(sa.func.sum(Document._length), 0).label("data_volume"),
        )
        .select_from(Source)
        .outerjoin(Document, Document.source_id == Source.id)
        .where(Source.project_id == project.id)
        .group_by(Source.id, Source.title)
        .order_by(Source.title)
    )
    source_docs_res = (await db.execute(source_docs_query)).all()

    # Query chunks grouped by source (via document)
    source_chunks_query = (
        sa.select(
            Source.id,
            sa.func.count(Chunk.id).label("chunk_count"),
            sa.func.coalesce(sa.func.sum(sa.func.length(Chunk.content)), 0).label(
                "chunk_storage"
            ),
        )
        .select_from(Source)
        .outerjoin(Document, Document.source_id == Source.id)
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .where(Source.project_id == project.id)
        .group_by(Source.id)
    )
    source_chunks_res = (await db.execute(source_chunks_query)).all()

    # Combine results
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
        "project": project,
        "labels": labels,
        "data_chats": data_chats,
        "data_users": data_users,
        "data_msgs": data_msgs,
        "data_hits": data_hits,
        "data_tokens": data_tokens,
        "total_chats": total_chats,
        "total_users": total_unique_users,
        "total_msgs": total_msgs,
        "total_hits": total_hits,
        "total_tokens": total_tokens,
        "token_breakdown": token_breakdown,
        "source_stats": source_stats,
        "total_docs": total_docs,
        "total_data_volume": total_data_volume,
        "total_chunks": total_chunks,
        "total_chunk_storage": total_chunk_storage,
    }


@login_required()
async def project_document_content(request):
    project_id = request.match_info.get("project_id")
    document_id = request.match_info.get("document_id")

    # Verify user has access to this project
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Get document
    document = await request["db"].scalar(
        sa.select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(
            Document.short_id == document_id,
            Source.project_id == project.id,
        )
    )

    if not document:
        raise web.HTTPNotFound()

    # Return the document content as HTML (markdown will be rendered by browser or we can use a markdown library)
    # For now, return as plain text wrapped in pre tag
    return web.Response(
        text=f'<pre class="whitespace-pre-wrap overflow-y-auto" style="max-height: 500px">{document.content}</pre>',
        content_type="text/html",
    )


@meta(title=_("Project Chat"))
@login_required()
@aiohttp_jinja2.template("chat/chat.html")
async def project_chat(request):
    project_id = request.match_info.get("project_id")
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    user_uid_param = request.rel_url.query.get("user_uid", "").strip()
    user_uid = user_uid_param or str(request["user"].id)

    chat = Chat(
        title=f"Chat for {project.title}",
        user_uid=user_uid,
        project_id=project.id,
        meta={},
        type="chat",
    )
    request["db"].add(chat)
    await request["db"].commit()
    await request["db"].refresh(chat)

    # Generate signed payload for WebSocket authentication
    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([request["user"].id, chat.short_id], salt="vchat")

    project_config = project.config or {}
    provider_obj, model_obj = resolve_ai_settings(
        project.provider,
        project.model,
    )
    ai_settings_url = request.app.router["project_actions"].url_for(
        action="update_ai_settings", item_id=project.short_id
    )

    return {
        "project": project,
        "chat": chat,
        "payload": payload,
        "agent_name": project_config.get("agent_name", ""),
        "welcome_message": project_config.get("welcome_message", ""),
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": True,
        "ai_settings_url": str(ai_settings_url),
    }


@meta(title=_("Integration"))
@login_required()
@aiohttp_jinja2.template("projects/integration.html")
async def project_integration(request):
    project_id = request.match_info.get("project_id")
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Ensure project secret exists
    if "secret" not in project.config:
        import secrets

        project.config["secret"] = secrets.token_urlsafe(32)
        # We need to re-assign to trigger update because it's a JSONB field
        flag_modified(project, "config")
        await request["db"].commit()

    return {
        "project": project,
        "project_secret": project.config["secret"],
    }


@meta(title=_("Chat Widget"))
@aiohttp_jinja2.template("chat/chat.html")
async def public_project_chat(request):
    project_id = request.match_info.get("project_id")
    # No login required, but we verify signature if provided
    project = await request["db"].scalar(
        sa.select(Project).where(Project.short_id == project_id)
    )
    if not project:
        raise web.HTTPNotFound()

    user_uid = request.query.get("user_uid", "").strip()
    user_name = request.query.get("user_name", "")
    user_email = request.query.get("user_email", "")
    sign = request.query.get("sign", "")

    if not user_uid:
        # Generate a temporary guest ID if not provided?
        # Or require it? The requirement says "copy and paste special javascript code... data-user-uid='...'"
        # So it should be provided. If not, maybe generate one stored in cookie?
        # For now, let's generate a random one if missing.
        import uuid

        user_uid = f"guest_{uuid.uuid4().hex[:8]}"

    # Verify signature if project has secret and sign is provided
    # Requirement: "integration sign also should be here if project want to use user attribution"
    # So if sign is present, we verify it.
    if sign and "secret" in project.config:
        import hashlib
        import hmac

        secret = project.config["secret"].encode("utf-8")
        msg = user_uid.encode("utf-8")
        expected_sign = hmac.new(secret, msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sign, sign):
            # Invalid signature
            # Should we block or just treat as unverified?
            # Let's log and maybe warn? Or just proceed as unverified?
            # For security, if they try to sign, it must be valid.
            return web.HTTPForbidden(text="Invalid signature")

    # Create or get chat
    # We need to find an existing active chat for this user_uid and project?
    # Or just create a new one?
    # The existing project_chat creates a new Chat every time.
    # Let's stick to that for now, or maybe reuse if recent?
    # Reusing would be better for user experience (refreshing page).
    # Let's try to find the latest chat for this user_uid in this project.

    # Check for existing recent chat (e.g. last 24 hours)
    # For now, let's just create a new one to match project_chat behavior,
    # but maybe we should improve this later.

    chat = Chat(
        title=f"Chat for {user_name or user_uid}",
        user_uid=user_uid,
        project_id=project.id,
        meta={"name": user_name, "email": user_email},
        type="chat",
    )
    request["db"].add(chat)
    await request["db"].commit()
    await request["db"].refresh(chat)

    # Generate signed payload for WebSocket authentication
    # The WebSocket handler expects [user_id, chat_id]
    # user_id can be string now.
    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([user_uid, chat.short_id], salt="vchat")

    provider_obj, model_obj = resolve_ai_settings(
        project.provider,
        project.model,
    )
    project_config = project.config or {}

    return {
        "project": project,
        "chat": chat,
        "payload": payload,
        "agent_name": project_config.get("agent_name", ""),
        "welcome_message": project_config.get("welcome_message", ""),
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": False,
        "ai_settings_url": None,
    }


@meta(title=_("Project Call"))
@login_required()
@aiohttp_jinja2.template("chat/call.html")
async def project_call(request):
    project_id = request.match_info.get("project_id")
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    user_uid_param = request.rel_url.query.get("user_uid", "").strip()
    user_uid = user_uid_param or str(request["user"].id)

    chat = Chat(
        title=f"Call for {project.title}",
        user_uid=user_uid,
        project_id=project.id,
        meta={},
        type="call",
    )
    request["db"].add(chat)
    await request["db"].commit()
    await request["db"].refresh(chat)

    return {"project": project, "chat": chat}


@meta(title=_("Project Files"))
@login_required()
@aiohttp_jinja2.template("projects/files.html")
async def project_files(request):
    project_id = request.match_info.get("project_id")
    db_session = request["db"]

    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Get 'uploaded_files' source
    source = await db_session.scalar(
        sa.select(Source).where(
            Source.project_id == project.id, Source.type == "upload"
        )
    )
    if not source:
        # create it
        source = Source(
            project_id=project.id,
            type="upload",
            title="Uploaded Files",
            uri="uploads://",
            config={},
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

    documents = (
        (
            await db_session.execute(
                sa.select(Document)
                .where(Document.source_id == source.id)
                .order_by(Document.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return {"project": project, "documents": documents, "upload_source": source}


@login_required()
async def secure_download(request):
    project_id = request.match_info.get("project_id")
    file_id = request.match_info.get("file_id")  # This is likely the document short_id
    db_session = request["db"]

    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    document = await db_session.scalar(
        sa.select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(
            Document.short_id == file_id,
            Source.project_id == project.id,
            Source.type == "upload",
        )
    )

    if not document:
        raise web.HTTPNotFound()

    file_path = document.uri
    if not os.path.exists(file_path):
        raise web.HTTPNotFound(text="File not found on disk")

    return web.FileResponse(file_path)


@login_required()
async def delete_file(request):
    project_id = request.match_info.get("project_id")
    file_id = request.match_info.get("file_id")
    db_session = request["db"]

    project = await db_session.scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    document = await db_session.scalar(
        sa.select(Document)
        .join(Source, Document.source_id == Source.id)
        .where(
            Document.short_id == file_id,
            Source.project_id == project.id,
            Source.type == "upload",
        )
    )

    if document:
        # Delete file from disk
        if document.uri and os.path.exists(document.uri):
            try:
                os.remove(document.uri)
            except OSError as e:
                logger.error(f"Error deleting file {document.uri}: {e}")

        await db_session.delete(document)
        await db_session.commit()

        return web.Response(text="ok")

    return web.HTTPNotFound()


async def on_upload(request: web.Request, resource: Any, source_path: Path) -> None:
    """
    Callback for aiotus when upload is complete.
    """
    project_id_str = request.match_info.get("project_id")
    if not project_id_str:
        logger.error("No project_id in upload URL")
        return

    db_session = request.get("db")

    project = await db_session.scalar(
        sa.select(Project).where(Project.short_id == project_id_str)
    )

    if not project:
        logger.error(f"Project {project_id_str} not found")
        return

    # Get or create 'uploaded_files' source
    source = await db_session.scalar(
        sa.select(Source).where(
            Source.project_id == project.id, Source.type == "upload"
        )
    )

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

    # Create a dummy document to get a short_id (or generate one)
    # We can't get short_id before insert easily with the current mixin unless we use a utility
    # Let's insert the document first with temp URI

    document = Document(
        source_id=source.id,
        title=original_filename,
        uri="",  # Will update
        content="",  # Will be indexed
        hash_value="",  # Will be updated
        meta={
            "filename": original_filename,
            "content_type": _decode_meta(metadata.get("filetype")),
            "doc_type": "file",  # generic
        },
        status="added",
    )
    db_session.add(document)
    await db_session.flush()  # to get ID and short_id

    new_filename = f"{document.short_id}{ext}"
    final_dir = source_path.parent  # Keep in the same project upload dir
    final_path = final_dir / new_filename

    # Rename/Move
    shutil.move(source_path, final_path)

    document.uri = str(final_path)
    document.meta["size_bytes"] = resource.file_size

    await db_session.commit()

    # Trigger indexing
    # We can use the pdf crawler logic or a generic file crawler
    # For now, let's assume we reuse the crawler task which will need to handle "upload" source
    # But wait, the crawler task usually crawls the whole source.
    # Here we just added one file.
    # We should probably trigger a specific document index task or just the source crawl task
    # which should be smart enough to only index new/changed files.
    # Given the existing architecture, let's trigger the file crawler for this upload.
    crawl_file_task.delay(document.id)
