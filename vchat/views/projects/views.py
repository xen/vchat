import logging
import re
import secrets
import json
import hashlib
import hmac
import uuid
from celery import chain
from celery.schedules import crontab
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlencode as urlencode_qs, urlparse
from zoneinfo import ZoneInfo

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import aliased

from jobs.crawler import (
    crawl_page_task,
    crawl_source_task,
    reapply_source_rules_task,
    sitemap_sync_task,
)
from jobs.crawler.tasks import (
    index_project,
    load_boilerplate_hashes,
    refresh_source_index,
    schedule_index_document,
    schedule_refresh_project_index,
)

from vchat.ai_providers import (
    DEFAULT_OPENAI_MODEL,
    get_ai_provider_options,
    get_default_model_id,
    is_model_available,
    is_provider_available,
    resolve_ai_settings,
)
from vchat.app_keys import CONFIG_KEY, SETTINGS_KEY, SIGNER_KEY
from vchat.chat_meta import merge_chat_meta
from vchat.document_types import DEFAULT_DOCUMENT_TYPE
from vchat.document_shingles import compute_trigram_hashes
from vchat.i18n import _
from vchat.models import Chat, ChatMsg, Chunk, Page, PageLink, Sitemap, Source, User
from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.project_settings import (
    apply_settings_updates,
    get_setting,
)
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_IGNORED_PARAMS,
    MANUAL_REINDEX_MODE,
    is_manual_reindex,
    normalize_reindex_cron,
)
from vchat.source_blocking import (
    apply_source_blocking_result,
    check_source_blocking,
    describe_blocked_reason,
)
from vchat.settings import config
from vchat.page_status import PageStatus, PageStatusError, STATUS_ERROR_DESCRIPTIONS
from vchat.utils import admin_event, flash, login_required, meta

from vchat.views.admin.views import CreateUserForm, UserPasswordForm

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
    "project_integration",
    "public_widget_chat",
    "project_files",
    "file_document",
    "source_sitemaps",
]


def _format_datetime_local(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _queue_source_crawl_from_ui(source_id: int) -> None:
    chain(
        sitemap_sync_task.si(source_id),
        crawl_source_task.si(source_id, skip_sitemap_sync=True),
    ).apply_async()


async def _check_source_blocking_and_commit(
    request: web.Request,
    db_session: Any,
    source: Source,
) -> bool:
    result = check_source_blocking(source.uri)
    apply_source_blocking_result(source, result)
    source.updated_at = datetime.now(timezone.utc)
    await db_session.commit()

    if result.is_blocked:
        await flash(
            request,
            result.message or _("Source is blocked for crawling"),
            "error",
        )
        return True
    return False


def _format_crawl_bool(value: bool) -> str:
    return "Да" if value else "Нет"


def _format_bytes_compact(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} МБ"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} ГБ"


def _document_content_size_bytes(document: Page) -> int:
    if document.content:
        return len(document.content.encode("utf-8"))
    return int(getattr(document, "_length", 0) or 0)


def _document_uniqueness_percent(
    document: Page, boilerplate_hashes: frozenset[int]
) -> int | None:
    content = (document.content or "").strip()
    if not content:
        return None

    hashes = compute_trigram_hashes(content)
    if not hashes:
        return 100
    if not boilerplate_hashes:
        return 100

    overlap = len(hashes & boilerplate_hashes)
    unique_ratio = max(0.0, 1.0 - (overlap / len(hashes)))
    return round(unique_ratio * 100)


async def _load_document_uniqueness_percent(db: Any, document: Page) -> int | None:
    if not document.content:
        return None
    if not document.source_id:
        return _document_uniqueness_percent(document, frozenset())

    boilerplate_hashes = await db.run_sync(
        lambda sync_db: load_boilerplate_hashes(sync_db, document.source_id)
    )
    return _document_uniqueness_percent(document, boilerplate_hashes)


def _document_stats_summary(
    document: Page,
    chunk_rows: list[Chunk],
    extraction: dict[str, Any],
    uniqueness_percent: int | None,
) -> str:
    parts = [
        _format_bytes_compact(_document_content_size_bytes(document)),
        f"{len(chunk_rows)} чанков",
        f"{int(extraction.get('word_count') or 0)} слов",
        f"{int(extraction.get('table_count') or 0)} таблиц",
    ]
    if uniqueness_percent is not None:
        parts.append(f"{uniqueness_percent}% уникальности текста")
    return ", ".join(parts)


def _document_crawl_summary(document: Page) -> str:
    parts = [
        f"код {document.http_status if document.http_status is not None else '—'}",
        f"обход {_format_datetime_local(document.last_crawled_at)}",
        f"изм. {_format_datetime_local(document.last_modified_at)}",
        f"интервал {document.check_interval_days} дн.",
        f"стабильность {document.stable_count}",
        f"ошибок {document.error_count}",
        f"входящих {document.inlink_count}",
    ]
    if document.last_etag:
        parts.insert(3, f"etag {document.last_etag}")
    return ", ".join(parts)


def _document_crawl_fields(document: Page) -> list[dict[str, str]]:
    return [
        {
            "label": "HTTP status",
            "value": str(document.http_status)
            if document.http_status is not None
            else "—",
        },
        {
            "label": "Последний обход",
            "value": _format_datetime_local(document.last_crawled_at),
        },
        {
            "label": "Последнее изменение",
            "value": _format_datetime_local(document.last_modified_at),
        },
        {
            "label": "ETag",
            "value": document.last_etag or "—",
        },
        {
            "label": "Интервал проверки, дней",
            "value": str(document.check_interval_days),
        },
        {
            "label": "Стабильных обходов подряд",
            "value": str(document.stable_count),
        },
        {
            "label": "Ошибок подряд",
            "value": str(document.error_count),
        },
        {
            "label": "Hub-страница",
            "value": _format_crawl_bool(document.is_hub_page),
        },
        {
            "label": "Ценность контента",
            "value": (
                f"{document.content_value:.2f}"
                if document.content_value is not None
                else "—"
            ),
        },
        {
            "label": "Входящих ссылок",
            "value": str(document.inlink_count),
        },
    ]


def _sort_document_link_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            (row.get("title") or "").casefold(),
            (row.get("uri") or "").casefold(),
        ),
    )


def _is_external_uri(uri: str | None, current_netloc: str | None) -> bool:
    if not uri or not current_netloc:
        return False
    return urlparse(uri).netloc.casefold() != current_netloc.casefold()


def _is_ignored_link_status(
    status: str | None, status_error: str | None = None
) -> bool:
    return (status or "") in {"blocked", "auth_required"} or (status_error or "") in {
        PageStatusError.excluded_ignored.value,
        PageStatusError.excluded_robots.value,
        PageStatusError.excluded_rules.value,
        PageStatusError.excluded_auth.value,
        PageStatusError.no_content.value,
        PageStatusError.low_content.value,
        PageStatusError.redirect.value,
    }


def _resolve_document_link_status(page: Page | Any | None) -> str:
    if page is None:
        return "missing"
    if getattr(page, "status_error", None) == PageStatusError.excluded_auth:
        return "auth_required"
    if getattr(page, "status_error", None) in (
        PageStatusError.excluded_robots,
        PageStatusError.excluded_rules,
    ):
        return "blocked"
    if getattr(page, "last_crawled_at", None) is None:
        return "not_indexed"
    return "ok"


def _document_links_graph(
    document: Page,
    document_display_title: str,
    link_groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    current_node_id = f"page-{document.id}"
    current_netloc = urlparse(document.uri).netloc if document.uri else None
    nodes: list[dict[str, Any]] = [
        {
            "id": current_node_id,
            "page_id": document.id,
            "title": document_display_title,
            "uri": document.uri,
            "relation": "current",
            "detail_url": f"/page/{document.id}",
            "status": document.status,
            "is_ignored": _is_ignored_link_status(None, document.status_error),
            "is_external": False,
        }
    ]
    links: list[dict[str, Any]] = []
    seen_nodes: set[str] = {current_node_id}
    seen_links: set[tuple[str, str, str]] = set()

    for relation, rows in link_groups.items():
        for row in rows:
            node_id = f"page-{row['id']}"
            if node_id not in seen_nodes:
                nodes.append(
                    {
                        "id": node_id,
                        "page_id": row["id"],
                        "title": row["title"],
                        "uri": row.get("uri"),
                        "relation": relation,
                        "detail_url": f"/page/{row['id']}",
                        "status": row.get("status"),
                        "is_ignored": _is_ignored_link_status(
                            row.get("status"), row.get("status_error")
                        ),
                        "is_external": _is_external_uri(row.get("uri"), current_netloc),
                    }
                )
                seen_nodes.add(node_id)

            if relation in {"incoming", "mutual"}:
                key = (node_id, current_node_id, "incoming")
                if key not in seen_links:
                    links.append(
                        {
                            "source": node_id,
                            "target": current_node_id,
                            "relation": "incoming",
                        }
                    )
                    seen_links.add(key)
            if relation in {"outgoing", "mutual"}:
                key = (current_node_id, node_id, "outgoing")
                if key not in seen_links:
                    links.append(
                        {
                            "source": current_node_id,
                            "target": node_id,
                            "relation": "outgoing",
                        }
                    )
                    seen_links.add(key)

    return {"currentNodeId": current_node_id, "nodes": nodes, "links": links}


async def _document_link_groups(
    db: Any, document: Page
) -> dict[str, list[dict[str, Any]]]:
    if document.id is None:
        return {"mutual": [], "incoming": [], "outgoing": []}

    outgoing_page = aliased(Page)
    incoming_page = aliased(Page)

    outgoing_rows = (
        await db.execute(
            sa.select(PageLink, outgoing_page)
            .outerjoin(outgoing_page, outgoing_page.id == PageLink.target_page_id)
            .where(PageLink.source_page_id == document.id)
        )
    ).all()
    incoming_rows = (
        await db.execute(
            sa.select(PageLink, incoming_page)
            .outerjoin(incoming_page, incoming_page.id == PageLink.source_page_id)
            .where(PageLink.target_page_id == document.id)
        )
    ).all()

    outgoing_by_id: dict[int, dict[str, Any]] = {}
    for link, linked_page in outgoing_rows:
        linked_id = link.target_page_id
        if linked_id is None or linked_id in outgoing_by_id:
            continue
        title = _display_document_title(
            getattr(linked_page, "title", None),
            link.target_uri,
        )
        outgoing_by_id[linked_id] = {
            "id": linked_id,
            "uri": link.target_uri,
            "title": title,
            "status": link.target_status or "unknown",
            "status_error": getattr(linked_page, "status_error", None),
        }

    incoming_by_id: dict[int, dict[str, Any]] = {}
    for _link, linked_page in incoming_rows:
        linked_id = getattr(linked_page, "id", None)
        linked_uri = getattr(linked_page, "uri", None)
        if linked_id is None or linked_id in incoming_by_id:
            continue
        title = _display_document_title(
            getattr(linked_page, "title", None),
            linked_uri,
        )
        incoming_by_id[linked_id] = {
            "id": linked_id,
            "uri": linked_uri,
            "title": title,
            "status": _resolve_document_link_status(linked_page),
            "status_error": getattr(linked_page, "status_error", None),
        }

    mutual_ids = set(outgoing_by_id) & set(incoming_by_id)
    mutual = _sort_document_link_rows(
        [
            {
                **outgoing_by_id[linked_id],
                "status": outgoing_by_id[linked_id].get("status") or "unknown",
            }
            for linked_id in mutual_ids
        ]
    )
    outgoing = _sort_document_link_rows(
        [
            row
            for linked_id, row in outgoing_by_id.items()
            if linked_id not in mutual_ids
        ]
    )
    incoming = _sort_document_link_rows(
        [
            row
            for linked_id, row in incoming_by_id.items()
            if linked_id not in mutual_ids
        ]
    )
    return {
        "mutual": mutual,
        "incoming": incoming,
        "outgoing": outgoing,
    }


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


def next_reindex_at(cron_expr: str, now: datetime) -> datetime | None:
    if is_manual_reindex(cron_expr):
        return None
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    try:
        schedule = crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
            nowfun=lambda: now,
        )
        return now + schedule.remaining_estimate(now)
    except ValueError:
        return None


def with_default_ignored_param_rules(
    rules: list[CrawlerRule] | None,
) -> list[CrawlerRule]:
    merged: list[CrawlerRule] = []
    seen: set[tuple[str, str]] = set()
    for param in DEFAULT_IGNORED_PARAMS:
        merged.append(CrawlerRule(type="param", value=param))
        seen.add(("param", param))
    for rule in rules or []:
        key = (rule.type, rule.value)
        if key not in seen:
            merged.append(rule)
            seen.add(key)
    return merged


_SLUG_RE = re.compile(r"^[a-z][a-z0-9\-_]*$")


def _is_slug_title(title: str | None) -> bool:
    return bool(title and len(title) >= 4 and _SLUG_RE.fullmatch(title))


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


def _document_pipeline_steps(document: Page) -> tuple[str, str | None, str | None]:
    """Return (status, status_error, error_message) for the pipeline widget."""
    status = document.status or PageStatus.crawler
    status_error = document.status_error
    meta = document.meta or {}

    if not status_error:
        return status, None, None

    try:
        err_enum = PageStatusError(status_error)
        msg = STATUS_ERROR_DESCRIPTIONS.get(err_enum, status_error)
    except ValueError:
        msg = status_error

    detail = str(meta.get("error") or meta.get("message") or "").strip()
    if detail and detail != msg:
        msg = f"{msg}: {detail}"

    return status, status_error, msg or None


async def _document_detail_context(request, document_id: int) -> dict[str, Any]:
    db = request["db"]
    document = await db.scalar(sa.select(Page).where(Page.id == document_id))
    if not document:
        raise web.HTTPNotFound()
    document_links = await _document_link_groups(db, document)
    document_display_title = _display_document_title(document.title, document.uri)

    chunk_rows = (
        (
            await db.execute(
                sa.select(Chunk)
                .where(Chunk.page_id == document.id)
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
    uniqueness_percent = await _load_document_uniqueness_percent(db, document)

    return {
        "project": _project_context(request),
        "document": document,
        "page_title": document_display_title,
        "document_display_title": document_display_title,
        "document_crawl_fields": _document_crawl_fields(document),
        "document_pipeline": _document_pipeline_steps(document),
        "document_stats_summary": _document_stats_summary(
            document,
            chunk_rows,
            extraction,
            uniqueness_percent,
        ),
        "document_crawl_summary": _document_crawl_summary(document),
        "document_uniqueness_percent": uniqueness_percent,
        "document_structure": structure,
        "document_outline": outline,
        "document_extraction": extraction,
        "document_chunks": chunk_rows,
        "document_links": document_links,
        "document_links_graph": _document_links_graph(
            document,
            document_display_title,
            document_links,
        ),
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
    )


async def _files_rows(db_session: Any) -> list[dict[str, Any]]:
    chunk_counts = (
        sa.select(
            Chunk.page_id.label("document_id"),
            sa.func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.page_id)
        .subquery()
    )
    size_bytes_expr = sa.func.coalesce(
        sa.cast(sa.func.octet_length(Page.content), sa.BigInteger),
        sa.cast(Page._length, sa.BigInteger),
        sa.literal(0, type_=sa.BigInteger),
    ).label("size_bytes")

    rows = (
        await db_session.execute(
            sa.select(Page, size_bytes_expr, chunk_counts.c.chunk_count)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Page.id)
            .where(Page.source_id.is_(None), Page.uri.is_(None))
            .order_by(Page.updated_at.desc(), Page.created_at.desc())
        )
    ).all()

    result: list[dict[str, Any]] = []
    for doc, size_bytes, chunk_count in rows:
        raw_meta = doc.meta if isinstance(doc.meta, dict) else {}
        author_email = raw_meta.get("author_email")
        if isinstance(author_email, str) and author_email.strip():
            author_display = author_email.strip()
        else:
            author_name = raw_meta.get("author_name")
            if isinstance(author_name, str) and author_name.strip():
                author_display = author_name.strip()
            else:
                author_display = "-"

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
                "author_display": author_display,
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


def _build_progress_conditions():
    _EXCLUDED_ERRORS = [
        PageStatusError.excluded_auth.value,
        PageStatusError.excluded_ignored.value,
        PageStatusError.excluded_robots.value,
        PageStatusError.excluded_rules.value,
        PageStatusError.no_content.value,
        PageStatusError.low_content.value,
        PageStatusError.redirect.value,
    ]
    is_excl = Page.status_error.in_(_EXCLUDED_ERRORS)
    is_err = sa.and_(
        Page.status_error.isnot(None),
        sa.not_(is_excl),
    )
    is_ready = sa.and_(
        Page.status == PageStatus.ready,
        Page.status_error.is_(None),
    )
    is_pend = sa.and_(
        Page.status == PageStatus.crawler,
        Page.status_error.is_(None),
    )
    is_proc = sa.and_(
        Page.status == PageStatus.parsing,
        Page.status_error.is_(None),
    )
    return is_excl, is_err, is_pend, is_ready, is_proc


def _serialize_source_row(row: Any) -> dict[str, Any]:
    source_name = row.title or row.uri or ""
    errors = int(row.errors or 0)
    pending = int(row.pending or 0)
    processing = int(row.processing or 0)
    ready = int(row.ready or 0)
    excluded = int(row.excluded or 0)
    return {
        "id": row.id,
        "title": source_name,
        "uri": row.uri,
        "is_paused": row.is_paused,
        "blocked_reason": row.blocked_reason,
        "blocked_message": row.blocked_message,
        "blocked_label": describe_blocked_reason(row.blocked_reason),
        "errors": errors,
        "pending": pending,
        "processing": processing,
        "ready": ready,
        "excluded": excluded,
        "page_url_base": "/page?" + urlencode_qs({"source": source_name}),
    }


async def _get_source_row_data(db_session, source_id: int) -> dict[str, Any] | None:
    is_excl, is_err, is_pend, is_ready, is_proc = _build_progress_conditions()
    row = (
        await db_session.execute(
            sa.select(
                Source.id,
                Source.title,
                Source.uri,
                Source.is_paused,
                Source.blocked_reason,
                Source.blocked_message,
                sa.func.count(Page.id).filter(is_excl).label("excluded"),
                sa.func.count(Page.id).filter(is_err).label("errors"),
                sa.func.count(Page.id).filter(is_pend).label("pending"),
                sa.func.count(Page.id).filter(is_proc).label("processing"),
                sa.func.count(Page.id).filter(is_ready).label("ready"),
            )
            .outerjoin(Page, Page.source_id == Source.id)
            .where(Source.id == source_id)
            .group_by(
                Source.id,
                Source.title,
                Source.uri,
                Source.is_paused,
                Source.blocked_reason,
                Source.blocked_message,
            )
        )
    ).one()
    if not getattr(row, "id", None):
        return None
    return _serialize_source_row(row)


@meta(title=_("Sources"))
@login_required()
@aiohttp_jinja2.template("projects/sources.html")
async def project_edit_sources(request):
    db_session = request["db"]

    is_excl, is_err, is_pend, is_ready, is_proc = _build_progress_conditions()

    # Per-source progress buckets
    source_rows = (
        await db_session.execute(
            sa.select(
                Source.id,
                Source.title,
                Source.uri,
                Source.is_paused,
                Source.blocked_reason,
                Source.blocked_message,
                sa.func.count(Page.id).filter(is_excl).label("excluded"),
                sa.func.count(Page.id).filter(is_err).label("errors"),
                sa.func.count(Page.id).filter(is_pend).label("pending"),
                sa.func.count(Page.id).filter(is_proc).label("processing"),
                sa.func.count(Page.id).filter(is_ready).label("ready"),
            )
            .outerjoin(Page, Page.source_id == Source.id)
            .group_by(
                Source.id,
                Source.title,
                Source.uri,
                Source.is_paused,
                Source.blocked_reason,
                Source.blocked_message,
            )
            .order_by(
                Source.is_paused.asc(),
                Source.blocked_reason.isnot(None).desc(),
                Source.created_at.desc(),
            )
        )
    ).all()

    # Overall stats
    overall_row = (
        await db_session.execute(
            sa.select(
                sa.func.count(Page.id).filter(is_excl).label("excluded"),
                sa.func.count(Page.id).filter(is_err).label("errors"),
                sa.func.count(Page.id).filter(is_pend).label("pending"),
                sa.func.count(Page.id).filter(is_proc).label("processing"),
                sa.func.count(Page.id).filter(is_ready).label("ready"),
            ).where(Page.source_id.isnot(None))
        )
    ).one()

    sources = [_serialize_source_row(row) for row in source_rows]

    session = await get_session(request)
    form = forms.SourceForm(meta={"csrf_context": session})

    overall_total = int(
        (overall_row.errors or 0)
        + (overall_row.pending or 0)
        + (overall_row.processing or 0)
        + (overall_row.ready or 0)
        + (overall_row.excluded or 0)
    )

    return {
        "project": _project_context(request),
        "sources": sources,
        "form": form,
        "overall": {
            "total": overall_total,
            "ready": int(overall_row.ready or 0),
            "errors": int(overall_row.errors or 0),
            "pending": int(overall_row.pending or 0),
            "processing": int(overall_row.processing or 0),
            "excluded": int(overall_row.excluded or 0),
        },
    }


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
        reapply_source_rules_task.delay(source.id)
        await admin_event("source_update", request)
        await flash(request, _("Source settings updated"), "success")
        raise web.HTTPFound(request.path)

    doc_stats_row = (
        await db_session.execute(
            sa.select(
                sa.func.count(Page.id).label("doc_count"),
                sa.func.coalesce(
                    sa.func.sum(
                        sa.func.coalesce(
                            sa.cast(sa.func.octet_length(Page.content), sa.BigInteger),
                            sa.cast(Page._length, sa.BigInteger),
                            sa.literal(0, type_=sa.BigInteger),
                        )
                    ),
                    0,
                ).label("doc_size_bytes"),
            ).where(Page.source_id == source_id)
        )
    ).one()

    chunk_stats_row = (
        await db_session.execute(
            sa.select(
                sa.func.count(Chunk.id).label("chunk_count"),
                sa.func.coalesce(
                    sa.func.sum(
                        sa.cast(sa.func.octet_length(Chunk.text), sa.BigInteger)
                    ),
                    0,
                ).label("chunk_size_bytes"),
            )
            .join(Page, Page.id == Chunk.page_id)
            .where(Page.source_id == source_id)
        )
    ).one()

    now = datetime.now(timezone.utc)
    return {
        "project": _project_context(request),
        "source": source,
        "source_blocked_label": describe_blocked_reason(source.blocked_reason),
        "form": form,
        "doc_count": int(doc_stats_row.doc_count or 0),
        "doc_size_bytes": int(doc_stats_row.doc_size_bytes or 0),
        "chunk_count": int(chunk_stats_row.chunk_count or 0),
        "chunk_size_bytes": int(chunk_stats_row.chunk_size_bytes or 0),
        "next_reindex": next_reindex_at(source.reindex_cron, now),
    }


@login_required()
async def source_sitemaps(request):
    """Return HTMX fragment: sitemap list for a source."""
    source_id = int(request.match_info.get("source_id"))
    db_session = request["db"]
    source = await db_session.scalar(sa.select(Source).where(Source.id == source_id))
    if not source:
        raise web.HTTPNotFound()

    sitemaps = (
        (
            await db_session.execute(
                sa.select(Sitemap)
                .where(Sitemap.source_id == source_id)
                .order_by(Sitemap.first_seen_at.asc())
            )
        )
        .scalars()
        .all()
    )

    return aiohttp_jinja2.render_template(
        "projects/_sitemaps.html",
        request,
        {"source": source, "sitemaps": sitemaps},
    )


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
        form = CreateUserForm(data, meta={"csrf_context": session})
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
        form = UserPasswordForm(data, meta={"csrf_context": session})

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
            sa.select(Page).where(Page.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound(text="Page not found")
        await db_session.delete(document)
        await db_session.commit()
        response = web.Response(text="")
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    if action == "ignore_document":
        document = await db_session.scalar(
            sa.select(Page).where(Page.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound(text="Page not found")
        data = await request.post()
        raw_value = data.get("is_ignored")
        currently_ignored = document.status_error == PageStatusError.excluded_ignored
        if raw_value is not None:
            want_ignored = str(raw_value).lower() in {"1", "true", "yes", "on"}
        else:
            want_ignored = not currently_ignored
        if want_ignored:
            document.status = PageStatus.crawler
            document.status_error = PageStatusError.excluded_ignored
        else:
            document.status_error = None
        await db_session.commit()
        response = web.json_response({"is_ignored": want_ignored})
        response.headers["HX-Trigger"] = "project-documents:refresh"
        return response

    if action == "delete_file":
        document = await db_session.scalar(
            sa.select(Page).where(Page.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound(text="Page not found")
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
            config=SourceConfig(rules=with_default_ignored_param_rules(rules)),
            reindex_cron=reindex_cron,
        )
        db_session.add(source)
        is_blocked = await _check_source_blocking_and_commit(
            request, db_session, source
        )
        await admin_event("source_create", request)
        if not is_blocked:
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

    if action == "crawl_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        if not await _check_source_blocking_and_commit(request, db_session, source):
            _queue_source_crawl_from_ui(source.id)
            await flash(request, _("Crawl task started for source"), "success")
        return web.Response(text="ok", status=200)

    if action == "refresh_page":
        document = await db_session.scalar(
            sa.select(Page).where(Page.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound()
        if not document.source_id or not document.uri:
            raise web.HTTPBadRequest(text="Page cannot be refreshed")

        meta = dict(document.meta or {})
        meta["force_reprocess_once"] = True
        document.meta = meta
        document.updated_at = datetime.now(timezone.utc)
        await db_session.commit()

        crawl_page_task.delay(document.id)
        await admin_event("page_refresh_request", request)
        await flash(
            request,
            _("Обновление страницы запущено"),
            "success",
        )
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
        source_ids = (
            (
                await db_session.execute(
                    sa.select(Source.id).where(
                        Source.is_paused.is_(False),
                        Source.blocked_reason.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for source_id in source_ids:
            _queue_source_crawl_from_ui(source_id)
        await flash(request, _("Crawl task started for all sources"), "success")
        return web.Response(text="ok", status=200)

    if action == "pause_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        source.is_paused = True
        source.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        await admin_event("source_pause", request)
        if request.headers.get("HX-Request", "").lower() == "true":
            source_row = await _get_source_row_data(db_session, int(item_id))
            html = aiohttp_jinja2.render_string(
                "projects/_source_toggle_button.html",
                request,
                {"s": source_row},
            )
            return web.Response(text=html, content_type="text/html")
        return web.Response(text="ok", status=200)

    if action == "resume_source":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        source.is_paused = False
        source.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        await admin_event("source_resume", request)
        if request.headers.get("HX-Request", "").lower() == "true":
            source_row = await _get_source_row_data(db_session, int(item_id))
            html = aiohttp_jinja2.render_string(
                "projects/_source_toggle_button.html",
                request,
                {"s": source_row},
            )
            return web.Response(text=html, content_type="text/html")
        return web.Response(text="ok", status=200)

    if action == "refresh_project_index":
        schedule_refresh_project_index()
        await flash(request, _("Update task started"), "success")
        return web.Response(text="ok", status=200)

    if action == "index_project":
        index_project.delay()
        await flash(request, _("Full rebuild task started"), "success")
        return web.Response(text="ok", status=200)

    if action == "delete_source_rule":
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == int(item_id))
        )
        if not source:
            raise web.HTTPNotFound()
        data = await request.post()
        try:
            rule_index = int(data.get("rule_index", "-1"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid rule index")
        cfg = source.config
        rules = list(cfg.rules)
        if rule_index < 0 or rule_index >= len(rules):
            raise web.HTTPBadRequest(text="Rule index out of range")
        rules.pop(rule_index)
        source.config = SourceConfig(
            crawler_concurrent_requests=cfg.crawler_concurrent_requests,
            crawler_download_delay=cfg.crawler_download_delay,
            crawler_download_timeout=cfg.crawler_download_timeout,
            rules=rules,
        )
        source.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        reapply_source_rules_task.delay(source.id)
        await admin_event("source_update", request)
        return web.Response(text="ok", status=200)

    if action == "sitemap_add":
        source_id = int(item_id)
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == source_id)
        )
        if not source:
            raise web.HTTPNotFound()
        data = await request.post()
        url = (data.get("url") or "").strip()
        if not url:
            raise web.HTTPBadRequest(text="URL required")
        existing = await db_session.scalar(
            sa.select(Sitemap).where(Sitemap.source_id == source_id, Sitemap.url == url)
        )
        if not existing:
            db_session.add(
                Sitemap(
                    source_id=source_id,
                    url=url,
                    discovered_via="manual",
                    first_seen_at=datetime.now(timezone.utc),
                )
            )
            await db_session.commit()
        sitemaps = (
            (
                await db_session.execute(
                    sa.select(Sitemap)
                    .where(Sitemap.source_id == source_id)
                    .order_by(Sitemap.first_seen_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return aiohttp_jinja2.render_template(
            "projects/_sitemaps.html",
            request,
            {"source": source, "sitemaps": sitemaps},
        )

    if action == "sitemap_toggle":
        sitemap = await db_session.scalar(
            sa.select(Sitemap).where(Sitemap.id == int(item_id))
        )
        if not sitemap:
            raise web.HTTPNotFound()
        sitemap.is_excluded = not sitemap.is_excluded
        await db_session.commit()
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == sitemap.source_id)
        )
        sitemaps = (
            (
                await db_session.execute(
                    sa.select(Sitemap)
                    .where(Sitemap.source_id == sitemap.source_id)
                    .order_by(Sitemap.first_seen_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return aiohttp_jinja2.render_template(
            "projects/_sitemaps.html",
            request,
            {"source": source, "sitemaps": sitemaps},
        )

    if action == "sitemap_delete":
        sitemap = await db_session.scalar(
            sa.select(Sitemap).where(Sitemap.id == int(item_id))
        )
        if not sitemap:
            raise web.HTTPNotFound()
        source_id = sitemap.source_id
        await db_session.delete(sitemap)
        await db_session.commit()
        source = await db_session.scalar(
            sa.select(Source).where(Source.id == source_id)
        )
        sitemaps = (
            (
                await db_session.execute(
                    sa.select(Sitemap)
                    .where(Sitemap.source_id == source_id)
                    .order_by(Sitemap.first_seen_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return aiohttp_jinja2.render_template(
            "projects/_sitemaps.html",
            request,
            {"source": source, "sitemaps": sitemaps},
        )

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
            Chunk.page_id.label("document_id"),
            sa.func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.page_id)
        .subquery()
    )

    size_bytes_expr = sa.cast(Page._length, sa.BigInteger).label("size_bytes")

    documents = (
        await request["db"].execute(
            sa.select(
                Page.id,
                Page.title,
                Page.uri,
                Page.status,
                Page.status_error,
                Source.title.label("source_title"),
                Source.uri.label("source_uri"),
                size_bytes_expr,
                chunk_counts.c.chunk_count,
            )
            .join(Source, Page.source_id == Source.id)
            .outerjoin(chunk_counts, chunk_counts.c.document_id == Page.id)
            .order_by(Page.created_at.desc())
        )
    ).all()

    data = []
    for (
        document_id,
        title,
        uri,
        status,
        status_error,
        source_title,
        source_uri,
        size_bytes,
        chunk_count,
    ) in documents:
        data.append(
            {
                "id": str(document_id),
                "title": _display_document_title(title, uri),
                "uri": uri or "",
                "source": source_title or source_uri or _("Файлы"),
                "status": status,
                "status_error": status_error,
                "is_ignored": status_error == PageStatusError.excluded_ignored,
                "size_bytes": int(size_bytes or 0),
                "chunk_count": int(chunk_count or 0),
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

    all_providers = get_ai_provider_options()
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
            sa.func.count(Page.id).label("doc_count"),
            sa.func.coalesce(sa.func.sum(Page._length), 0).label("data_volume"),
        )
        .select_from(Source)
        .outerjoin(Page, Page.source_id == Source.id)
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
        .outerjoin(Page, Page.source_id == Source.id)
        .outerjoin(Chunk, Chunk.page_id == Page.id)
        .group_by(Source.id)
    )
    source_chunks_res = (await db.execute(source_chunks_query)).all()
    files_docs_row = (
        await db.execute(
            sa.select(
                sa.func.count(Page.id).label("doc_count"),
                sa.func.coalesce(sa.func.sum(Page._length), 0).label("data_volume"),
            ).where(Page.source_id.is_(None))
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
            .select_from(Page)
            .outerjoin(Chunk, Chunk.page_id == Page.id)
            .where(Page.source_id.is_(None))
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

    # Refresh stale slug-titles from the document table
    slug_uris = {
        src["uri"]
        for msg in initial_messages
        for src in msg.get("sources") or []
        if src.get("uri") and _is_slug_title(src.get("title"))
    }
    if slug_uris:
        fresh_rows = (
            await request["db"].execute(
                sa.select(Page.uri, Page.title).where(Page.uri.in_(slug_uris))
            )
        ).all()
        uri_title_map = {r.uri: r.title for r in fresh_rows if r.title}
        for msg in initial_messages:
            for src in msg.get("sources") or []:
                fresh = uri_title_map.get(src.get("uri"))
                if fresh:
                    src["title"] = fresh
                    src["display_path"] = fresh

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
        "allow_ai_switch": False,
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
        document = Page(
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
            status=PageStatus.parsing,
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
        "active_section": str(request.app.router["project_files"].url_for()),
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
        sa.select(Page).where(
            Page.id == document_id,
            Page.source_id.is_(None),
            Page.uri.is_(None),
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
        document.status = PageStatus.parsing
        document.status_error = None
        document.updated_at = datetime.now(timezone.utc)
        await db_session.execute(sa.delete(Chunk).where(Chunk.page_id == document.id))
        await db_session.commit()
        schedule_index_document(document.id)
        await admin_event("file_update", request)
        await flash(request, _("Файл сохранен"), "success")
        raise web.HTTPFound(location=request.path)

    files_rows = await _files_rows(db_session)

    return {
        "project": _project_context(request),
        "active_section": str(request.app.router["project_files"].url_for()),
        "files_rows": files_rows,
        "current_document": document,
    }
