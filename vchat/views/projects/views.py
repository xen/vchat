import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
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
from itsdangerous import BadSignature, URLSafeSerializer
from passlib.context import CryptContext
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased, defer
from sqlalchemy.orm.attributes import set_committed_value

from jobs.crawler.tasks import (
    async_update_page_shingles,
    crawl_page_task,
    crawl_source_task,
    index_project,
    load_boilerplate_hashes,
    reapply_source_rules_task,
    refresh_source_index,
    schedule_index_document,
    schedule_refresh_project_index,
    sitemap_sync_task,
)
from jobs.triggers.tasks import generate_missing_triggers_task

from vchat.views.chat.ai import (
    DEFAULT_OPENAI_MODEL,
    get_ai_provider_options,
    resolve_ai_settings,
)
from vchat.views.chat.sources import enrich_source_payloads
from vchat.settings import CONFIG_KEY, REDIS_KEY, SETTINGS_KEY, SIGNER_KEY
from vchat.widget_state import (
    WIDGET_STATE_DISABLED,
    WIDGET_STATE_ENABLED,
    WIDGET_STATE_MISSING,
    cache_widget_state,
)
from vchat.views.chat.meta import merge_chat_meta
from jobs.documents.types import DEFAULT_DOCUMENT_TYPE
from jobs.documents.content import document_too_big_message, is_document_too_big
from jobs.documents.shingles import compute_trigram_hashes
from vchat.utils import json_response
from vchat.models import (
    Chat,
    ChatMsg,
    Chunk,
    LLMCacheEntry,
    Page,
    PageLink,
    Sitemap,
    Source,
    TriggerResponseCache,
    ApiClient,
    WidgetIntegration,
    User,
    UserSession,
)
from vchat.models.data import api_client_source
from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.views.projects.settings import (
    apply_settings_updates,
    get_setting,
)
from jobs.crawler.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_IGNORED_PARAMS,
    MANUAL_REINDEX_MODE,
    is_manual_reindex,
    normalize_reindex_cron,
)
from jobs.crawler.source_blocking import (
    apply_source_blocking_result,
    check_source_blocking,
    describe_blocked_reason,
)
from vchat.settings import config
from vchat.views.projects.page_status import (
    EXCLUDED_INDEX_STATUS_ERRORS,
    PageStatus,
    PageStatusError,
    STATUS_ERROR_DESCRIPTIONS,
)
from vchat.views.triggers.rules import (
    DEFAULT_SOURCE_TRIGGER_PATTERN,
    TRIGGER_DEFAULTS_SETTING,
    TriggerPatternError,
    apply_source_trigger_rules,
    load_default_trigger_templates,
    page_trigger_items,
    trigger_pattern_matches_url,
    trigger_rule_url_part,
    validate_trigger_pattern,
)
from vchat.utils import (
    admin_event,
    flash,
    htmx_required,
    login_required,
    meta,
    validate_signed_user_csrf,
)

from vchat.views.admin.views import (
    ApiClientAdd,
    ApiClientEdit,
    UserAdd,
    UserPasswordEdit,
)
from vchat.views.api.views import decrypt_client_secret, encrypt_client_secret

from . import forms

logger = logging.getLogger(__name__)
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")
DOCUMENT_CONTENT_PREVIEW_CHARS = 500

__all__ = [
    "index",
    "project_action",
    "project_edit_sources",
    "project_source_settings",
    "project_view",
    "project_document_content",
    "project_document_content_rest",
    "project_document_detail",
    "project_documents_csv",
    "project_files_json",
    "project_chat",
    "project_stats",
    "project_integration",
    "project_widget_edit",
    "project_llm_cache",
    "project_triggers",
    "project_trigger_rule_count",
    "public_widget_chat",
    "project_files",
    "file_document",
    "source_sitemaps",
]


async def _revoke_user_sessions(
    request,
    db_session,
    *,
    where_clause,
    reason: str,
    event_name: str,
) -> web.Response:
    now = datetime.now(timezone.utc)
    await db_session.execute(
        sa.update(UserSession)
        .where(where_clause, UserSession.revoked_at.is_(None))
        .values(
            revoked_at=now,
            revoked_reason=reason,
            updated_at=now,
        )
    )
    await db_session.commit()
    await admin_event(event_name, request)
    response = web.Response(text="ok")
    response.headers["HX-Refresh"] = "true"
    return response


async def _api_client_list_context(
    request: web.Request,
    db_session,
    *,
    add_form: ApiClientAdd,
    new_credentials=None,
    selected_source_ids: set[int] | None = None,
) -> dict[str, Any]:
    clients = (
        (await db_session.execute(sa.select(ApiClient).order_by(ApiClient.id.desc())))
        .scalars()
        .all()
    )
    if clients:
        client_ids = [client.id for client in clients]
        source_rows = (
            await db_session.execute(
                sa.select(
                    api_client_source.c.api_client_id,
                    Source.id,
                    Source.title,
                    Source.uri,
                )
                .join(Source, Source.id == api_client_source.c.source_id)
                .where(api_client_source.c.api_client_id.in_(client_ids))
                .order_by(Source.title.asc(), Source.id.asc())
            )
        ).all()
        sources_by_client: dict[int, list[SimpleNamespace]] = {}
        for client_id, source_id, title, uri in source_rows:
            sources_by_client.setdefault(client_id, []).append(
                SimpleNamespace(id=source_id, title=title, uri=uri)
            )
        for client in clients:
            client.sources = sources_by_client.get(client.id, [])
            secret = decrypt_client_secret(
                client.encrypted_secret,
                request.app[CONFIG_KEY]["secret_key"],
            )
            client.masked_secret = f"vchatsec-...{secret[-4:]}"

    source_rows = (
        await db_session.execute(
            sa.select(Source.id, Source.title, Source.uri).order_by(
                Source.title.asc(),
                Source.id.asc(),
            )
        )
    ).all()
    sources = (
        SimpleNamespace(id=source_id, title=title, uri=uri)
        for source_id, title, uri in source_rows
    )
    return {
        "clients": clients,
        "sources": list(sources),
        "add_form": add_form,
        "new_credentials": new_credentials,
        "selected_source_ids": selected_source_ids or set(),
    }


def _format_datetime_local(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def _public_widget_url(code: str) -> str:
    return f"https://chat.vbudushee.ru/widget/{code}"


def _new_widget_code() -> str:
    return secrets.token_urlsafe(8).rstrip("_-")


def _new_widget_secret() -> str:
    return secrets.token_urlsafe(32)


async def _assign_new_widget_code(db_session, widget: WidgetIntegration) -> None:
    code = _new_widget_code()
    while await db_session.scalar(
        sa.select(WidgetIntegration.id).where(WidgetIntegration.code == code)
    ):
        code = _new_widget_code()
    widget.code = code


def _ensure_widget_secret(widget: WidgetIntegration | SimpleNamespace) -> None:
    if not getattr(widget, "secret", ""):
        widget.secret = _new_widget_secret()


async def _cache_widget_enabled_state(request, widget: WidgetIntegration) -> None:
    state = WIDGET_STATE_ENABLED if widget.is_enabled else WIDGET_STATE_DISABLED
    await cache_widget_state(request.app[REDIS_KEY], widget.code, state)


async def _widget_integrations(db_session) -> list[WidgetIntegration]:
    return (
        (
            await db_session.execute(
                sa.select(WidgetIntegration).order_by(WidgetIntegration.id.desc())
            )
        )
        .scalars()
        .all()
    )


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
    result = check_source_blocking(
        source.uri,
        ignore_robots_txt=source.config.ignore_robots_txt,
    )
    apply_source_blocking_result(source, result)
    source.updated_at = datetime.now(timezone.utc)
    await db_session.commit()

    if result.is_blocked:
        await flash(
            request,
            result.message or "Источник заблокирован для обхода",
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


def _document_content_size_bytes(
    document: Page, content_size_bytes: int | None = None
) -> int:
    if content_size_bytes is not None:
        return int(content_size_bytes or 0)
    if document.content:
        return len(document.content.encode("utf-8"))
    return int(getattr(document, "_length", 0) or 0)


def _document_uniqueness_percent(
    content: str, boilerplate_hashes: frozenset[int]
) -> int | None:
    content = (content or "").strip()
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


def _document_stats_summary(
    document: Page,
    chunk_rows: list[Chunk],
    extraction: dict[str, Any],
    uniqueness_percent: int | None,
    content_size_bytes: int | None = None,
) -> str:
    parts = [
        _format_bytes_compact(
            _document_content_size_bytes(document, content_size_bytes)
        ),
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
    status_error_value = getattr(status_error, "value", status_error)
    return (status or "") in {"blocked", "auth_required"} or (
        status_error_value or ""
    ) in EXCLUDED_INDEX_STATUS_ERRORS


def _is_ignored_document(document: Page | Any) -> bool:
    return _is_ignored_link_status(
        getattr(document, "status", None),
        getattr(document, "status_error", None),
    )


def _document_content_preview_at_word_boundary(
    content_preview: str,
    *,
    is_truncated: bool,
) -> tuple[str, int]:
    if not is_truncated:
        return content_preview, len(content_preview)

    preview = content_preview[:DOCUMENT_CONTENT_PREVIEW_CHARS].rstrip()
    if not preview:
        return "", 0

    last_whitespace = max(preview.rfind(" "), preview.rfind("\n"), preview.rfind("\t"))
    if last_whitespace <= 0:
        return preview, len(preview)

    preview = preview[:last_whitespace].rstrip()
    return preview, len(preview)


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
            "status": _resolve_document_link_status(linked_page),
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


def _message_suggested_actions(row: ChatMsg) -> list[str]:
    if row.role != "assistant" or not row.full_context:
        return []
    try:
        payload = json.loads(row.full_context)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    actions = payload.get("suggested_actions")
    if not isinstance(actions, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_action in actions:
        if not isinstance(raw_action, str):
            continue
        action = raw_action.strip()
        if not action:
            continue
        key = action.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(action)
        if len(normalized) >= 3:
            break
    return normalized


async def _initial_messages_for_chat(
    db,
    *,
    chat: Chat,
    serializer: URLSafeSerializer,
) -> list[dict[str, Any]]:
    history_rows = (
        (
            await db.execute(
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
                "suggested_actions": _message_suggested_actions(row),
            }
        )

    slug_uris = {
        src["uri"]
        for msg in initial_messages
        for src in msg.get("sources") or []
        if isinstance(src, dict)
        and src.get("uri")
        and _is_slug_title(src.get("title"))
    }
    if slug_uris:
        fresh_rows = (
            await db.execute(sa.select(Page.uri, Page.title).where(Page.uri.in_(slug_uris)))
        ).all()
        uri_title_map = {r.uri: r.title for r in fresh_rows if r.title}
        for msg in initial_messages:
            for src in msg.get("sources") or []:
                if not isinstance(src, dict):
                    continue
                fresh = uri_title_map.get(src.get("uri"))
                if fresh:
                    src["title"] = fresh
                    src["display_path"] = fresh

    for msg in initial_messages:
        sources = msg.get("sources") or []
        if sources:
            msg["sources"] = await enrich_source_payloads(db, sources)

    return initial_messages


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
    content_char_count_expr = sa.func.coalesce(
        sa.func.length(Page.content),
        sa.cast(Page._length, sa.Integer),
        sa.literal(0, type_=sa.Integer),
    )
    empty_json_object = sa.cast(sa.literal("{}"), JSONB)
    empty_json_array = sa.cast(sa.literal("[]"), JSONB)
    document_row = (
        await db.execute(
            sa.select(
                Page,
                sa.func.coalesce(
                    sa.func.substring(
                        Page.content,
                        1,
                        DOCUMENT_CONTENT_PREVIEW_CHARS,
                    ),
                    "",
                ).label("content_preview"),
                sa.func.coalesce(
                    sa.cast(sa.func.octet_length(Page.content), sa.BigInteger),
                    sa.cast(Page._length, sa.BigInteger),
                    sa.literal(0, type_=sa.BigInteger),
                ).label("content_size_bytes"),
                content_char_count_expr.label("content_char_count"),
                sa.func.coalesce(
                    Page.meta["extraction"],
                    empty_json_object,
                ).label("meta_extraction"),
                sa.func.coalesce(
                    Page.meta["outline"],
                    empty_json_array,
                ).label("meta_outline"),
                sa.case(
                    (
                        content_char_count_expr <= DOCUMENT_CONTENT_PREVIEW_CHARS,
                        sa.func.coalesce(Page.meta["structure"], empty_json_array),
                    ),
                    else_=empty_json_array,
                ).label("meta_structure"),
                Page.meta["removed_shingles"]
                .as_string()
                .label("meta_removed_shingles"),
                Page.meta["reason"].as_string().label("meta_reason"),
                Page.meta["message"].as_string().label("meta_message"),
                Page.meta["error"].as_string().label("meta_error"),
                Page.meta["exception_class"].as_string().label("meta_exception_class"),
                Page.meta["duplicate_of_page_id"].label("meta_duplicate_of_page_id"),
            )
            .options(defer(Page.content), defer(Page.meta))
            .where(Page.id == document_id)
        )
    ).one_or_none()
    if not document_row:
        raise web.HTTPNotFound()
    (
        document,
        document_content_preview,
        document_content_size_bytes,
        document_content_char_count,
        meta_extraction,
        meta_outline,
        meta_structure,
        meta_removed_shingles,
        meta_reason,
        meta_message,
        meta_error,
        meta_exception_class,
        meta_duplicate_of_page_id,
    ) = document_row
    document_content_preview = document_content_preview or ""
    document_content_size_bytes = int(document_content_size_bytes or 0)
    document_content_char_count = int(document_content_char_count or 0)
    document_content_is_truncated = (
        document_content_char_count > DOCUMENT_CONTENT_PREVIEW_CHARS
    )
    (
        document_content_preview,
        document_content_preview_source_chars,
    ) = _document_content_preview_at_word_boundary(
        document_content_preview,
        is_truncated=document_content_is_truncated,
    )
    document_content_can_expand = (
        document_content_is_truncated
        and (
            document.status_error == PageStatusError.duplicate_content
            or not _is_ignored_document(document)
        )
    )
    document_meta = {
        "extraction": meta_extraction if isinstance(meta_extraction, dict) else {},
        "outline": meta_outline if isinstance(meta_outline, list) else [],
        "structure": meta_structure if isinstance(meta_structure, list) else [],
    }
    for key, value in (
        ("removed_shingles", meta_removed_shingles),
        ("reason", meta_reason),
        ("message", meta_message),
        ("error", meta_error),
        ("exception_class", meta_exception_class),
        ("duplicate_of_page_id", meta_duplicate_of_page_id),
    ):
        if value:
            document_meta[key] = value
    set_committed_value(document, "meta", document_meta)
    document_links = await _document_link_groups(db, document)
    document_display_title = _display_document_title(document.title, document.uri)
    document_duplicate = None
    duplicate_page_id = None
    if meta_duplicate_of_page_id:
        try:
            duplicate_page_id = int(meta_duplicate_of_page_id)
        except (TypeError, ValueError):
            duplicate_page_id = None
    if duplicate_page_id is not None:
        duplicate_row = (
            await db.execute(
                sa.select(Page.id, Page.title, Page.uri).where(
                    Page.id == duplicate_page_id
                )
            )
        ).one_or_none()
        if duplicate_row is not None:
            duplicate_id, duplicate_title, duplicate_uri = duplicate_row
            document_duplicate = {
                "id": duplicate_id,
                "title": _display_document_title(duplicate_title, duplicate_uri),
                "detail_url": f"/page/{duplicate_id}",
            }
        else:
            document_duplicate = {
                "id": duplicate_page_id,
                "title": f"Документ #{duplicate_page_id}",
                "detail_url": f"/page/{duplicate_page_id}",
            }
    document_source = None
    document_triggers_enabled = False
    if document.source_id:
        document_source = await db.scalar(
            sa.select(Source).where(Source.id == document.source_id)
        )
        document_triggers_enabled = bool(
            document_source and document_source.enable_triggers
        )

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
    raw_extraction = raw_meta.get("extraction")
    extraction: dict[str, Any] = raw_extraction if isinstance(raw_extraction, dict) else {}
    uniqueness_percent = None
    if not document_content_is_truncated:
        boilerplate_hashes = frozenset()
        source_id = document.source_id
        if source_id is not None:
            boilerplate_hashes = await db.run_sync(
                lambda sync_db: load_boilerplate_hashes(sync_db, source_id)
            )
        uniqueness_percent = _document_uniqueness_percent(
            document_content_preview,
            boilerplate_hashes,
        )
    trigger_rows = page_trigger_items(document) if document_triggers_enabled else []
    trigger_keys = [trigger["key"] for trigger in trigger_rows]
    cache_rows = []
    if trigger_keys:
        cache_rows = (
            (
                await db.execute(
                    sa.select(TriggerResponseCache)
                    .where(TriggerResponseCache.page_id == document.id)
                    .where(TriggerResponseCache.trigger_key.in_(trigger_keys))
                    .order_by(TriggerResponseCache.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    caches_by_trigger: dict[str, list[TriggerResponseCache]] = {}
    for cache in cache_rows:
        caches_by_trigger.setdefault(cache.trigger_key, []).append(cache)

    document_triggers = [
        {
            "trigger": trigger,
            "caches": caches_by_trigger.get(trigger["key"], []),
        }
        for trigger in trigger_rows
    ]

    return {
        "document": document,
        "page_title": document_display_title,
        "document_display_title": document_display_title,
        "document_content_preview": document_content_preview,
        "document_content_is_truncated": document_content_is_truncated,
        "document_content_can_expand": document_content_can_expand,
        "document_content_preview_chars": DOCUMENT_CONTENT_PREVIEW_CHARS,
        "document_content_preview_source_chars": document_content_preview_source_chars,
        "document_content_char_count": document_content_char_count,
        "document_content_remaining_chars": max(
            document_content_char_count - document_content_preview_source_chars,
            0,
        ),
        "document_crawl_fields": _document_crawl_fields(document),
        "document_pipeline": _document_pipeline_steps(document),
        "document_duplicate": document_duplicate,
        "document_stats_summary": _document_stats_summary(
            document,
            chunk_rows,
            extraction,
            uniqueness_percent,
            document_content_size_bytes,
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
        "document_triggers_enabled": document_triggers_enabled,
        "document_triggers": document_triggers,
    }


def _project_context(request) -> SimpleNamespace:
    settings = request.app.get(SETTINGS_KEY, {})
    return SimpleNamespace(
        id="global",
        title=settings.get("project.title") or "vchat",
        provider=config.get("chat_provider") or "gigachat",
        model=(
            config.get("chat_model")
            or config.get("openai_model")
            or DEFAULT_OPENAI_MODEL
        ),
        system_prompt=forms.DEFAULT_SYSTEM_PROMPT,
        agent_style=settings.get("project.agent_style") or "",
        config={
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


@meta(title="Страницы")
@login_required()
@aiohttp_jinja2.template("projects/view.html")
async def index(request):
    return await project_view(request)


def _trigger_lines(raw: str | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        value = line.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _compile_trigger_patterns(patterns: list[str]) -> None:
    for pattern in patterns:
        validate_trigger_pattern(pattern)


def _source_trigger_display(source: Source) -> dict[str, str]:
    parsed = urlparse(source.uri or "")
    host = parsed.netloc or source.uri or ""
    title = (source.title or "").strip()
    if not title or title == host or title == (source.uri or "").rstrip("/"):
        return {"name": host, "hint": "", "full_uri": source.uri or ""}
    return {"name": title, "hint": host, "full_uri": source.uri or ""}


async def _count_source_trigger_pattern(
    db_session: Any,
    *,
    source: Source,
    pattern: str,
) -> int:
    validate_trigger_pattern(pattern)
    value = pattern.strip()
    if not value:
        return 0
    rows = (
        await db_session.execute(
            sa.select(Page.uri)
            .where(Page.source_id == source.id)
            .where(Page.uri.is_not(None))
        )
    ).scalars()
    return sum(
        1
        for uri in rows
        if trigger_pattern_matches_url(
            trigger_rule_url_part(source.uri, uri or ""), value
        )
    )


async def _load_llm_cache_context(request: web.Request) -> dict[str, Any]:
    db_session = request["db"]
    entries = (
        (
            await db_session.execute(
                sa.select(LLMCacheEntry)
                .order_by(
                    LLMCacheEntry.last_seen_at.desc(),
                    LLMCacheEntry.id.desc(),
                )
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    total_entries = await db_session.scalar(sa.select(sa.func.count(LLMCacheEntry.id)))
    enabled_entries = await db_session.scalar(
        sa.select(sa.func.count(LLMCacheEntry.id)).where(
            LLMCacheEntry.is_enabled.is_(True)
        )
    )
    observed_total = await db_session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(LLMCacheEntry.observed_count), 0))
    )
    potential_saved_requests = await db_session.scalar(
        sa.select(
            sa.func.coalesce(sa.func.sum(LLMCacheEntry.potential_saved_requests), 0)
        )
    )
    potential_saved_tokens = await db_session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(LLMCacheEntry.potential_saved_tokens), 0))
    )
    await db_session.rollback()
    return {
        "active_section": str(request.app.router["project_llm_cache"].url_for()),
        "entries": entries,
        "stats": {
            "total_entries": int(total_entries or 0),
            "enabled_entries": int(enabled_entries or 0),
            "observed_total": int(observed_total or 0),
            "potential_saved_requests": int(potential_saved_requests or 0),
            "potential_saved_tokens": int(potential_saved_tokens or 0),
        },
    }


@meta(title="LLM-кеш")
@login_required()
@aiohttp_jinja2.template("projects/llm_cache.html")
async def project_llm_cache(request):
    return await _load_llm_cache_context(request)


async def _load_trigger_settings_context(request, form=None) -> dict[str, Any]:
    db_session = request["db"]
    sources = list(
        (
            await db_session.execute(
                sa.select(Source).order_by(Source.title.asc(), Source.uri.asc())
            )
        ).scalars()
    )
    source_trigger_rows = []
    source_options = []
    for source in sources:
        display = _source_trigger_display(source)
        if source.enable_triggers:
            source_options.append(
                {
                    "id": source.id,
                    "name": display["name"],
                    "hint": display["hint"],
                    "full_uri": display["full_uri"],
                }
            )
        rule_rows = []
        for rule in source.config.trigger_rules:
            rule_rows.append(
                {
                    "value": rule.value,
                    "affected_count": await _count_source_trigger_pattern(
                        db_session,
                        source=source,
                        pattern=rule.value,
                    ),
                }
            )
        if not rule_rows:
            continue
        source_trigger_rows.append(
            {
                "source_id": source.id,
                "source_display": display,
                "rule_rows": rule_rows,
            }
        )
    if form is None:
        form = forms.TriggerEdit(
            meta={"csrf_context": await get_session(request)},
            data={
                "default_templates": "\n".join(
                    load_default_trigger_templates(request.app)
                )
            },
        )
    trigger_pages = list(
        (
            await db_session.execute(
                sa.select(Page.triggers).where(Page.has_triggers.is_(True))
            )
        ).scalars()
    )
    total_triggers = sum(
        len(items) for items in trigger_pages if isinstance(items, list)
    )
    pages_with_triggers = await db_session.scalar(
        sa.select(sa.func.count(Page.id)).where(Page.has_triggers.is_(True))
    )
    cached_responses = await db_session.scalar(
        sa.select(sa.func.count(TriggerResponseCache.id))
    )
    await db_session.rollback()
    return {
        "active_section": str(request.app.router["project_triggers"].url_for()),
        "form": form,
        "source_trigger_rows": source_trigger_rows,
        "source_options": source_options,
        "stats": {
            "total_triggers": int(total_triggers or 0),
            "pages_with_triggers": int(pages_with_triggers or 0),
            "cached_responses": int(cached_responses or 0),
        },
    }


@meta(title="Триггеры")
@login_required()
@aiohttp_jinja2.template("projects/triggers.html")
async def project_triggers(request):
    db_session = request["db"]
    session = await get_session(request)
    data = await request.post() if getattr(request, "method", "GET") == "POST" else None
    form = forms.TriggerEdit(
        meta={"csrf_context": session},
        formdata=data,
        data={
            "default_templates": "\n".join(load_default_trigger_templates(request.app))
        },
    )

    if data is not None:
        if form.validate():
            default_templates = _trigger_lines(form.default_templates.data)
            await apply_settings_updates(
                request.app,
                db_session,
                {TRIGGER_DEFAULTS_SETTING: default_templates},
            )
            matched = 0
            sources = list((await db_session.execute(sa.select(Source))).scalars())
            for source in sources:
                source_patterns = [
                    value.strip()
                    for value in data.getall(f"source_trigger_rules_{source.id}[]", [])
                    if value.strip()
                ]
                try:
                    _compile_trigger_patterns(source_patterns)
                except (TriggerPatternError, re.error) as exc:
                    await flash(request, f"Некорректный regex: {exc}", "error")
                    return await _load_trigger_settings_context(request, form)

                cfg = source.config
                source.config = SourceConfig(
                    crawler_concurrent_requests=cfg.crawler_concurrent_requests,
                    crawler_download_delay=cfg.crawler_download_delay,
                    crawler_download_timeout=cfg.crawler_download_timeout,
                    ignore_robots_txt=cfg.ignore_robots_txt,
                    rules=cfg.rules,
                    trigger_rules=[
                        CrawlerRule(type="regex", value=pattern)
                        for pattern in source_patterns
                    ],
                )
                source.updated_at = datetime.now(timezone.utc)
                await apply_source_trigger_rules(db_session, source)
                matched += (
                    await db_session.scalar(
                        sa.select(sa.func.count(Page.id)).where(
                            Page.source_id == source.id,
                            Page.has_triggers.is_(True),
                        )
                    )
                    or 0
                )
            await db_session.commit()
            await flash(
                request,
                f"Настройки сохранены. Страниц под правилами: {matched}",
                "success",
            )
            raise web.HTTPFound(request.app.router["project_triggers"].url_for())
        return await _load_trigger_settings_context(request, form)

    return await _load_trigger_settings_context(request, form)

@login_required()
async def project_trigger_rule_count(request):
    db_session = request["db"]
    try:
        source_id = int(request.query.get("source_id", "0"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid source_id")
    pattern = (request.query.get("pattern") or "").strip()
    if not pattern:
        return json_response({"ok": True, "count": None})

    source = await db_session.scalar(sa.select(Source).where(Source.id == source_id))
    if source is None:
        raise web.HTTPNotFound(text="Source not found")

    try:
        count = await _count_source_trigger_pattern(
            db_session,
            source=source,
            pattern=pattern,
        )
    except (TriggerPatternError, re.error) as exc:
        return json_response({"ok": False, "error": str(exc)}, status=400)
    return json_response({"ok": True, "count": count})


def _build_progress_conditions():
    is_excl = Page.status_error.in_(EXCLUDED_INDEX_STATUS_ERRORS)
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


@meta(title="Источники")
@login_required()
@aiohttp_jinja2.template("projects/sources.html")
async def project_edit_sources(request):
    db_session = request["db"]
    session = await get_session(request)
    data = (
        await request.post()
        if getattr(request, "method", "GET") == "POST"
        else None
    )
    form = forms.SourceAdd(
        formdata=data,
        meta={"csrf_context": session},
    )

    if data is not None and form.validate():
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
            enable_triggers=bool(form.enable_triggers.data),
        )
        db_session.add(source)
        is_blocked = await _check_source_blocking_and_commit(
            request, db_session, source
        )
        await admin_event("source_create", request)
        if not is_blocked:
            crawl_source_task.delay(source.id)
        raise web.HTTPFound(request.path)

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

    overall_total = int(
        (overall_row.errors or 0)
        + (overall_row.pending or 0)
        + (overall_row.processing or 0)
        + (overall_row.ready or 0)
        + (overall_row.excluded or 0)
    )

    context = {
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

    if data is not None:
        return aiohttp_jinja2.render_template(
            "projects/sources.html",
            request,
            context,
            status=400,
        )
    return context


@meta(title="Настройки источника")
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
    data = await request.post() if request.method == "POST" else None
    if data is not None:
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
            "ignore_robots_txt": cfg.ignore_robots_txt,
            "enable_triggers": source.enable_triggers,
        }

    form = forms.SourceSettingsEdit(**form_kwargs)

    if data is not None and form.validate():
        current_cfg = source.config
        source.title = form.title.data
        source.reindex_cron = normalize_reindex_cron(form.reindex_cron.data)
        source.updated_at = datetime.now(timezone.utc)
        source.uri = form.url.data
        source.enable_triggers = bool(form.enable_triggers.data)
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
            ignore_robots_txt=bool(form.ignore_robots_txt.data),
            rules=rules,
            trigger_rules=current_cfg.trigger_rules
            or (
                [CrawlerRule(type="regex", value=DEFAULT_SOURCE_TRIGGER_PATTERN)]
                if source.enable_triggers
                else []
            ),
        )
        if source.config.ignore_robots_txt:
            if source.blocked_reason == "robots_txt":
                source.blocked_reason = None
                source.blocked_message = None
                source.blocked_checked_at = None
            source.robots_cache = None

        await apply_source_trigger_rules(db_session, source)

        await db_session.commit()
        reapply_source_rules_task.delay(source.id)
        await admin_event("source_update", request)
        await flash(request, "Настройки источника обновлены", "success")
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
        "item": source,
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
@htmx_required(
    exempt_actions={
        "user_create",
        "user_password",
        "api_client_create",
        "api_client_update",
    }
)
async def project_action(request):
    db_session = request["db"]
    item_id = request.match_info.get("item_id")
    action = request.match_info.get("action")

    if action == "user_create":
        admin_user_create_enabled = request.app[CONFIG_KEY].get(
            "admin_user_create_enabled", True
        )
        if not admin_user_create_enabled:
            raise web.HTTPForbidden(text="User creation is disabled")

        session = await get_session(request)
        data = await request.post()
        form = UserAdd(data, meta={"csrf_context": session})
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
                    "admin_user_create_enabled": admin_user_create_enabled,
                },
                status=400,
            )

        email = form.email.data.strip().lower()
        exists = await db_session.scalar(sa.select(User.id).where(User.email == email))
        if exists:
            email_field: Any = form.email
            email_field.errors = list(form.email.errors)
            email_errors: Any = email_field.errors
            email_errors.append("Этот email уже используется")
            return aiohttp_jinja2.render_template(
                "admin/user_list.html",
                request,
                {
                    "users": users,
                    "add_form": form,
                    "total_users": len(users),
                    "current_user_id": request["user"].id,
                    "admin_user_create_enabled": admin_user_create_enabled,
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
        await flash(request, "Пользователь создан", "success")
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
        form = UserPasswordEdit(data, meta={"csrf_context": session})

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
            await flash(request, "Пароль обновлён", "success")
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response

        return aiohttp_jinja2.render_template(
            "admin/user_password_modal.html",
            request,
            {"form": form, "target_user": user_obj},
        )

    if action == "user_revoke_sessions":
        target_user_id = int(item_id)
        user_obj = await db_session.scalar(
            sa.select(User).where(User.id == target_user_id)
        )
        if not user_obj:
            raise web.HTTPNotFound()

        return await _revoke_user_sessions(
            request,
            db_session,
            where_clause=UserSession.user_id == target_user_id,
            reason="admin_revoke",
            event_name="user_sessions_revoke",
        )

    if action == "user_revoke_all_sessions":
        return await _revoke_user_sessions(
            request,
            db_session,
            where_clause=sa.true(),
            reason="admin_revoke_all",
            event_name="user_sessions_revoke_all",
        )

    if action == "user_delete":
        target_user_id = int(item_id)
        is_htmx = request.headers.get("HX-Request", "").lower() == "true"

        if target_user_id == request["user"].id:
            message = "Нельзя удалить самого себя"
            if is_htmx:
                return web.Response(text=message, status=400)
            await flash(request, message, "error")
            raise web.HTTPFound(request.app.router["users"].url_for())

        total_users = await db_session.scalar(sa.select(sa.func.count(User.id))) or 0
        if total_users <= 1:
            message = "Нельзя удалить последнего пользователя"
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

        await flash(request, "Пользователь удалён", "success")
        raise web.HTTPFound(request.app.router["users"].url_for())

    if action == "api_client_create":
        session = await get_session(request)
        data = await request.post()
        form = ApiClientAdd(data, meta={"csrf_context": session})
        source_ids = [
            int(source_id)
            for source_id in data.getall("source_ids", [])
            if str(source_id).isdigit()
        ]

        if not form.validate():
            return aiohttp_jinja2.render_template(
                "admin/api_client_list.html",
                request,
                await _api_client_list_context(
                    request,
                    db_session,
                    add_form=form,
                    selected_source_ids=set(source_ids),
                ),
                status=400,
            )

        valid_source_ids = (
            [
                source_id
                for (source_id,) in (
                    await db_session.execute(
                        sa.select(Source.id).where(Source.id.in_(source_ids))
                    )
                ).all()
            ]
            if source_ids
            else []
        )
        client_id = f"vchatid-{secrets.token_hex(8)}"
        client_secret = f"vchatsec-{secrets.token_urlsafe(32)}"
        client = ApiClient(
            name=form.name.data.strip(),
            client_id=client_id,
            encrypted_secret=encrypt_client_secret(
                client_secret,
                request.app[CONFIG_KEY]["secret_key"],
            ),
            is_active=True,
        )
        db_session.add(client)
        await db_session.flush()
        if valid_source_ids:
            await db_session.execute(
                api_client_source.insert(),
                [
                    {"api_client_id": client.id, "source_id": source_id}
                    for source_id in valid_source_ids
                ],
            )
        await db_session.commit()
        await admin_event("api_client_create", request)
        return aiohttp_jinja2.render_template(
            "admin/api_client_list.html",
            request,
            await _api_client_list_context(
                request,
                db_session,
                add_form=ApiClientAdd(meta={"csrf_context": session}),
                new_credentials=SimpleNamespace(
                    message="Клиент {name} создан".format(name=client.name),
                    client_id=client_id,
                    client_secret=client_secret,
                ),
            ),
        )

    if action == "api_client_update":
        session = await get_session(request)
        client_id = int(item_id)
        client = await db_session.scalar(
            sa.select(ApiClient).where(ApiClient.id == client_id)
        )
        if not client:
            raise web.HTTPNotFound()

        data = await request.post()
        reset_secret = data.get("reset_secret") == "1"
        if reset_secret:
            validate_signed_user_csrf(request)
            form = ApiClientEdit(data, meta={"csrf": False})
        else:
            form = ApiClientEdit(data, meta={"csrf_context": session})
        source_ids = [
            int(source_id)
            for source_id in data.getall("source_ids", [])
            if str(source_id).isdigit()
        ]
        if not form.validate():
            return aiohttp_jinja2.render_template(
                "admin/api_client_list.html",
                request,
                await _api_client_list_context(
                    request,
                    db_session,
                    add_form=ApiClientAdd(meta={"csrf_context": session}),
                    selected_source_ids=set(source_ids),
                ),
                status=400,
            )

        valid_source_ids = (
            [
                source_id
                for (source_id,) in (
                    await db_session.execute(
                        sa.select(Source.id).where(Source.id.in_(source_ids))
                    )
                ).all()
            ]
            if source_ids
            else []
        )

        client.name = form.name.data.strip()
        await db_session.execute(
            api_client_source.delete().where(
                api_client_source.c.api_client_id == client.id
            )
        )
        if valid_source_ids:
            await db_session.execute(
                api_client_source.insert(),
                [
                    {"api_client_id": client.id, "source_id": source_id}
                    for source_id in valid_source_ids
                ],
            )

        client_secret = None
        if reset_secret:
            client_secret = f"vchatsec-{secrets.token_urlsafe(32)}"
            client.encrypted_secret = encrypt_client_secret(
                client_secret,
                request.app[CONFIG_KEY]["secret_key"],
            )

        await db_session.commit()
        await admin_event("api_client_update", request)
        return aiohttp_jinja2.render_template(
            "admin/api_client_list.html",
            request,
            await _api_client_list_context(
                request,
                db_session,
                add_form=ApiClientAdd(meta={"csrf_context": session}),
                new_credentials=(
                    SimpleNamespace(
                        message="Секрет клиента {name} сброшен".format(
                            name=client.name
                        ),
                        client_id=client.client_id,
                        client_secret=client_secret,
                    )
                    if client_secret
                    else None
                ),
            ),
        )

    if action == "api_client_delete":
        client_id = int(item_id)
        client = await db_session.scalar(
            sa.select(ApiClient).where(ApiClient.id == client_id)
        )
        if not client:
            raise web.HTTPNotFound()

        await db_session.delete(client)
        await db_session.commit()
        await admin_event("api_client_delete", request)

        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "generate_triggers":
        generate_missing_triggers_task.delay()
        await flash(request, "Генерация триггеров запущена", "success")
        response = web.Response(text="ok")
        response.headers["HX-Trigger"] = "project-triggers:refresh"
        return response

    if action == "clear_triggers":
        trigger_rules = Source.__table__.c.config["trigger_rules"]
        source_ids = (
            sa.select(Source.id)
            .where(sa.func.jsonb_typeof(trigger_rules) == "array")
            .where(sa.func.jsonb_array_length(trigger_rules) > 0)
        )
        page_ids = sa.select(Page.id).where(Page.source_id.in_(source_ids))
        await db_session.execute(
            sa.delete(TriggerResponseCache).where(
                TriggerResponseCache.page_id.in_(page_ids)
            )
        )
        await db_session.execute(
            sa.update(Page)
            .where(Page.source_id.in_(source_ids))
            .where(Page.triggers.is_not(None))
            .values(triggers=None, updated_at=datetime.now(timezone.utc))
        )
        await db_session.commit()
        await flash(
            request,
            "Триггеры и кэши ответов очищены",
            "success",
        )
        response = web.Response(text="ok")
        response.headers["HX-Trigger"] = "project-triggers:refresh"
        return response

    if action in {"llm_cache_enable", "llm_cache_disable", "llm_cache_delete"}:
        cache_entry_id = int(item_id)
        cache_entry = await db_session.scalar(
            sa.select(LLMCacheEntry).where(LLMCacheEntry.id == cache_entry_id)
        )
        if cache_entry is None:
            raise web.HTTPNotFound()

        if action == "llm_cache_delete":
            await db_session.delete(cache_entry)
            event_name = "llm_cache_delete"
            message = "Запись LLM-кеша удалена"
        elif action == "llm_cache_disable":
            cache_entry.is_enabled = False
            cache_entry.disabled_reason = "manual"
            cache_entry.updated_at = datetime.now(timezone.utc)
            event_name = "llm_cache_disable"
            message = "Запись LLM-кеша отключена"
        else:
            cache_entry.is_enabled = True
            cache_entry.disabled_reason = None
            cache_entry.updated_at = datetime.now(timezone.utc)
            event_name = "llm_cache_enable"
            message = "Запись LLM-кеша включена"

        await db_session.commit()
        await admin_event(event_name, request)
        await flash(request, message, "success")
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "llm_cache_clear":
        await db_session.execute(sa.delete(LLMCacheEntry))
        await db_session.commit()
        await admin_event("llm_cache_clear", request)
        await flash(request, "Реестр LLM-кеша очищен", "success")
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "widget_reset_code":
        widget_id = int(item_id)
        widget = await db_session.scalar(
            sa.select(WidgetIntegration).where(WidgetIntegration.id == widget_id)
        )
        if not widget:
            raise web.HTTPNotFound()

        old_code = widget.code
        await _assign_new_widget_code(db_session, widget)
        widget.updated_at = datetime.now(timezone.utc)

        await db_session.commit()
        await cache_widget_state(request.app[REDIS_KEY], old_code, WIDGET_STATE_MISSING)
        await _cache_widget_enabled_state(request, widget)
        await admin_event("widget_reset_code", request)
        await flash(request, "Код виджета сброшен", "success")
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "widget_reset_secret":
        widget_id = int(item_id)
        widget = await db_session.scalar(
            sa.select(WidgetIntegration).where(WidgetIntegration.id == widget_id)
        )
        if not widget:
            raise web.HTTPNotFound()

        widget.secret = _new_widget_secret()
        widget.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        await admin_event("widget_reset_secret", request)
        await flash(request, "Секрет виджета сброшен", "success")
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action in {"widget_disable", "widget_enable"}:
        widget_id = int(item_id)
        widget = await db_session.scalar(
            sa.select(WidgetIntegration).where(WidgetIntegration.id == widget_id)
        )
        if not widget:
            raise web.HTTPNotFound()

        widget.is_enabled = action == "widget_enable"
        widget.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        await _cache_widget_enabled_state(request, widget)
        await admin_event(action, request)
        await flash(
            request,
            "Виджет включен" if widget.is_enabled else "Виджет отключен",
            "success",
        )
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    if action == "widget_delete":
        widget_id = int(item_id)
        widget = await db_session.scalar(
            sa.select(WidgetIntegration).where(WidgetIntegration.id == widget_id)
        )
        if not widget:
            raise web.HTTPNotFound()

        code = widget.code
        await db_session.delete(widget)
        await db_session.commit()
        await cache_widget_state(request.app[REDIS_KEY], code, WIDGET_STATE_MISSING)
        await admin_event("widget_delete", request)

        return web.Response(text="ok")

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
            {"project_secret": secret},
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
        response = json_response({"is_ignored": want_ignored})
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
            await flash(request, "Задача обхода источника запущена", "success")
        return web.Response(text="ok", status=200)

    if action == "refresh_page":
        document = await db_session.scalar(
            sa.select(Page).where(Page.id == int(item_id))
        )
        if not document:
            raise web.HTTPNotFound()
        if not document.source_id or not document.uri:
            raise web.HTTPBadRequest(text="Page cannot be refreshed")

        document.patch_meta(force_reprocess_once=True)
        document.updated_at = datetime.now(timezone.utc)
        await db_session.commit()

        crawl_page_task.delay(document.id)
        await admin_event("page_refresh_request", request)
        await flash(
            request,
            "Обновление страницы запущено",
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
            "Задача обновления запущена для %(title)s"
            % {"title": source.title or source.uri},
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
        await flash(request, "Задача обхода всех источников запущена", "success")
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
        await flash(request, "Задача обновления запущена", "success")
        return web.Response(text="ok", status=200)

    if action == "index_project":
        index_project.delay()
        await flash(request, "Полная переиндексация запущена", "success")
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
            ignore_robots_txt=cfg.ignore_robots_txt,
            rules=rules,
            trigger_rules=cfg.trigger_rules,
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


@meta(title="Страницы")
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
        "sources": sources,
        "source_filters": source_filters,
    }


DOCUMENTS_CSV_FIELDS = (
    "id",
    "title",
    "uri",
    "source",
    "status",
    "status_error",
    "is_ignored",
    "size_bytes",
    "chunk_count",
)

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _neutralize_csv_cell(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith(CSV_FORMULA_PREFIXES):
        return value
    return f"'{value}"


def _documents_csv_response(rows: list[dict[str, Any]]) -> web.Response:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=DOCUMENTS_CSV_FIELDS, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(
        {
            field: _neutralize_csv_cell(row.get(field, ""))
            for field in DOCUMENTS_CSV_FIELDS
        }
        for row in rows
    )
    return web.Response(text=buffer.getvalue(), content_type="text/csv")


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


@login_required()
async def project_documents_csv(request):
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
            .order_by(Page.id.desc())
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
                "source": source_title or source_uri or "Файлы",
                "status": _enum_value(status),
                "status_error": _enum_value(status_error),
                "is_ignored": "1"
                if status_error == PageStatusError.excluded_ignored
                else "0",
                "size_bytes": int(size_bytes or 0),
                "chunk_count": int(chunk_count or 0),
            }
        )

    return _documents_csv_response(data)


@login_required()
async def project_files_json(request):
    rows = await _files_rows(request["db"])
    return json_response([_file_row_to_payload(row) for row in rows])


@meta(title="Статистика")
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
            sa.select(sa.func.count(Chunk.id))
            .where(Chunk.embedding.is_(None))
            .where(Chunk.is_duplicate.is_(False))
        )
        or 0
    )

    return {
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
    }


@login_required()
@aiohttp_jinja2.template("projects/document_content.html")
async def project_document_content(request):
    document_id = int(request.match_info.get("document_id"))
    return await _document_detail_context(request, document_id)


@login_required()
@aiohttp_jinja2.template("projects/document_content_rest.html")
async def project_document_content_rest(request):
    document_id = int(request.match_info.get("document_id"))
    try:
        offset = int(
            request.rel_url.query.get("offset", str(DOCUMENT_CONTENT_PREVIEW_CHARS))
        )
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="offset must be an integer")
    if offset < 0:
        raise web.HTTPBadRequest(text="offset must be non-negative")

    db = request["db"]
    row = (
        await db.execute(
            sa.select(
                Page,
                sa.func.coalesce(
                    sa.func.substring(
                        Page.content,
                        offset + 1,
                    ),
                    "",
                ).label("content_rest"),
            )
            .options(defer(Page.content), defer(Page.meta))
            .where(Page.id == document_id)
        )
    ).one_or_none()
    if not row:
        raise web.HTTPNotFound()

    document, document_content_rest = row
    if (
        document.status_error != PageStatusError.duplicate_content
        and _is_ignored_document(document)
    ):
        raise web.HTTPNotFound()

    return {
        "document_content_rest": document_content_rest or "",
    }


@meta(title="Структура документа")
@login_required()
@aiohttp_jinja2.template("projects/document_detail.html")
async def project_document_detail(request):
    document_id = int(request.match_info.get("document_id"))
    return await _document_detail_context(request, document_id)


@meta(title="Чат")
@login_required()
@aiohttp_jinja2.template("chat/chat.html")
async def project_chat(request):
    chat_id = (request.match_info.get("chat_id") or "").strip()
    if chat_id:
        chat = await request["db"].scalar(sa.select(Chat).where(Chat.id == chat_id))
        if not chat:
            raise web.HTTPNotFound(text="Chat not found")
        chat.meta = merge_chat_meta(
            chat.meta,
            request,
            {"source_page_url": request.rel_url.query.get("source_page_url")},
        )
        await request["db"].commit()
    else:
        user_uid_param = request.rel_url.query.get("user_uid", "").strip()
        user_uid = user_uid_param or str(request["user"].id)

        project = _project_context(request)
        chat = Chat(
            title=f"Chat for {project.title}",
            user_uid=user_uid,
            meta=merge_chat_meta(
                {},
                request,
                {"source_page_url": request.rel_url.query.get("source_page_url")},
            ),
        )
        request["db"].add(chat)
        await request["db"].commit()
        await request["db"].refresh(chat)
        location = request.app.router["project_chat_with_id"].url_for(chat_id=chat.id)
        raise web.HTTPFound(location=location)

    serializer = URLSafeSerializer(config.get("secret_key"))
    payload = serializer.dumps([request["user"].id, chat.id], salt="vchat")
    signed_chat_id = serializer.dumps(chat.id, salt="chat")
    initial_messages = await _initial_messages_for_chat(
        request["db"],
        chat=chat,
        serializer=serializer,
    )

    project = _project_context(request)
    provider_obj, model_obj = resolve_ai_settings(project.provider, project.model)

    return {
        "project": project,
        "chat": chat,
        "widget": SimpleNamespace(
            agent_name="",
            welcome_messages=list(forms.WIDGET_WELCOME_MESSAGES),
            waiting_messages=list(forms.WIDGET_WAITING_MESSAGES),
            error_message=forms.WIDGET_ERROR_MESSAGE,
            footer_text=forms.WIDGET_FOOTER_TEXT,
            pinned_messages=[],
        ),
        "payload": payload,
        "agent_name": "",
        "welcome_message": "",
        "footer_text": forms.WIDGET_FOOTER_TEXT,
        "error_message": forms.WIDGET_ERROR_MESSAGE,
        "pinned_messages": [],
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": False,
        "ai_settings_url": None,
        "initial_messages": initial_messages,
        "signed_chat_id": signed_chat_id,
    }


@meta(title="Код виджета")
@login_required()
@aiohttp_jinja2.template("projects/integration.html")
async def project_integration(request):
    session = await get_session(request)
    secret = get_setting(request.app, "project.secret", "") or ""
    if request.method == "POST":
        data = await request.post()
        form = forms.WidgetIntegrationAdd(
            formdata=data,
            meta={"csrf_context": session},
        )
        if not form.validate():
            return {
                "project_secret": secret,
                "form": form,
                "widgets": await _widget_integrations(request["db"]),
            }

        widget = WidgetIntegration(code="")
        form.populate_obj(widget)
        _ensure_widget_secret(widget)
        widget.is_enabled = True
        widget.welcome_messages = list(forms.WIDGET_WELCOME_MESSAGES)
        widget.waiting_messages = list(forms.WIDGET_WAITING_MESSAGES)
        widget.error_message = forms.WIDGET_ERROR_MESSAGE
        widget.footer_text = forms.WIDGET_FOOTER_TEXT
        widget.system_prompt = forms.DEFAULT_SYSTEM_PROMPT
        widget.suggestions_prompt = forms.DEFAULT_SUGGESTIONS_PROMPT
        await _assign_new_widget_code(request["db"], widget)
        request["db"].add(widget)
        await request["db"].flush()
        widget_id = widget.id
        await request["db"].commit()
        await _cache_widget_enabled_state(request, widget)
        await admin_event("widget_create", request)
        await flash(request, "Код виджета создан", "success")
        raise web.HTTPFound(
            request.app.router["project_widget_edit"].url_for(
                widget_id=str(widget_id)
            )
        )

    if not secret:
        secret = secrets.token_urlsafe(32)
        await apply_settings_updates(
            request.app, request["db"], {"project.secret": secret}
        )
        await request["db"].commit()

    return {
        "project_secret": secret,
        "form": forms.WidgetIntegrationAdd(meta={"csrf_context": session}),
        "widgets": await _widget_integrations(request["db"]),
    }


@meta(title="Редактировать код виджета")
@login_required()
@aiohttp_jinja2.template("projects/widget_edit.html")
async def project_widget_edit(request):
    widget_id = int(request.match_info["widget_id"])
    item = await request["db"].scalar(
        sa.select(WidgetIntegration).where(WidgetIntegration.id == widget_id)
    )
    if not item:
        await request["db"].rollback()
        raise web.HTTPNotFound()

    session = await get_session(request)
    formdata = await request.post() if request.method == "POST" else None
    form = forms.WidgetIntegrationEdit(
        formdata=formdata,
        obj=item if formdata is None else None,
        meta={"csrf_context": session},
    )
    _ensure_widget_secret(item)
    item.public_url = _public_widget_url(getattr(item, "code", ""))

    if request.method == "POST":
        if not form.validate():
            return {"item": item, "form": form}
        item.name = form.name.data
        item.agent_name = form.agent_name.data
        item.system_prompt = form.system_prompt.data
        item.suggestions_enabled = form.suggestions_enabled.data
        item.suggestions_prompt = form.suggestions_prompt.data
        item.error_message = form.error_message.data
        item.footer_text = form.footer_text.data
        item.welcome_messages = form.cleaned_welcome_messages
        item.waiting_messages = form.cleaned_waiting_messages
        item.pinned_messages = form.cleaned_pinned_messages
        item.updated_at = datetime.now(timezone.utc)
        await request["db"].commit()
        await admin_event("widget_update", request)
        await flash(request, "Код виджета обновлен", "success")
        raise web.HTTPFound(
            request.app.router["project_widget_edit"].url_for(
                widget_id=str(widget_id)
            )
        )

    return {
        "item": item,
        "form": form,
    }


async def _widget_integration_by_code(request, code: str) -> SimpleNamespace:
    widget = await request["db"].scalar(
        sa.select(WidgetIntegration).where(WidgetIntegration.code == code)
    )
    if not widget:
        await request["db"].rollback()
        raise web.HTTPNotFound(text="Widget code not found")
    return widget


WIDGET_USER_INFO_MAX_AGE_SECONDS = 3600
WIDGET_USER_INFO_CLOCK_SKEW_SECONDS = 300
WIDGET_USER_INFO_ALLOWED_KEYS = {
    "user_uid",
    "user_name",
    "user_email",
    "timestamp",
    "signature",
}


def _widget_user_info_signature_payload(data: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in data.items() if key != "signature"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_widget_user_info_value(
    data: dict[str, Any],
    key: str,
    *,
    max_length: int,
    required: bool = False,
) -> str:
    value = data.get(key, "")
    if value == "" and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if not value and required:
        raise ValueError(f"{key} is required")
    if value != value.strip():
        raise ValueError(f"{key} must not have surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{key} is too long")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{key} contains control characters")
    if "<" in value or ">" in value:
        raise ValueError(f"{key} contains forbidden characters")
    return value


def _load_signed_widget_user_info(
    raw_user_info: str,
    widget: WidgetIntegration | SimpleNamespace,
) -> dict[str, str]:
    if len(raw_user_info) > 4096:
        raise ValueError("user_info is too long")
    try:
        data = json.loads(raw_user_info)
    except json.JSONDecodeError as exc:
        raise ValueError("user_info is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("user_info must be a JSON object")
    unknown_keys = set(data) - WIDGET_USER_INFO_ALLOWED_KEYS
    if unknown_keys:
        raise ValueError("user_info contains unknown keys")
    if not {"user_uid", "timestamp", "signature"}.issubset(data):
        raise ValueError("user_info misses required keys")

    user_uid = _validate_widget_user_info_value(
        data, "user_uid", max_length=256, required=True
    )
    user_name = _validate_widget_user_info_value(data, "user_name", max_length=200)
    user_email = _validate_widget_user_info_value(data, "user_email", max_length=254)
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, int):
        raise ValueError("timestamp must be an integer")
    now = int(datetime.now(timezone.utc).timestamp())
    if timestamp > now + WIDGET_USER_INFO_CLOCK_SKEW_SECONDS:
        raise ValueError("timestamp is in the future")
    if now - timestamp > WIDGET_USER_INFO_MAX_AGE_SECONDS:
        raise ValueError("timestamp is expired")

    signature = data.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("signature must be a SHA-256 hex digest")
    secret = getattr(widget, "secret", "") or ""
    if not secret:
        raise ValueError("widget secret is not configured")
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        _widget_user_info_signature_payload(data).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise ValueError("signature is invalid")

    return {
        "user_uid": user_uid,
        "user_name": user_name,
        "user_email": user_email,
    }


async def _render_public_chat(request, widget: SimpleNamespace):
    user_info = request.query.get("user_info", "").strip()
    if user_info:
        try:
            signed_user_info = _load_signed_widget_user_info(user_info, widget)
        except ValueError:
            return web.HTTPForbidden(text="Invalid user info")
        user_uid = signed_user_info["user_uid"]
        user_name = signed_user_info["user_name"]
        user_email = signed_user_info["user_email"]
    else:
        guest_uid = request.query.get("guest_uid", "").strip()
        user_uid = guest_uid if re.fullmatch(r"guest_[0-9a-f]{8}", guest_uid) else ""
        user_name = ""
        user_email = ""
    source_page_url = request.query.get("source_page_url", "")
    signed_resume_chat_id = request.query.get("chat_id", "").strip()

    if not user_uid:
        user_uid = f"guest_{uuid.uuid4().hex[:8]}"

    serializer = URLSafeSerializer(config.get("secret_key"))
    if signed_resume_chat_id:
        try:
            resume_chat_id = serializer.loads(signed_resume_chat_id, salt="chat")
        except BadSignature:
            return web.HTTPForbidden(text="Invalid chat id")
        chat = await request["db"].scalar(sa.select(Chat).where(Chat.id == resume_chat_id))
        if not chat:
            raise web.HTTPNotFound(text="Chat not found")
        if chat.user_uid != user_uid or (chat.meta or {}).get("widget_code") != widget.code:
            return web.HTTPForbidden(text="Invalid chat id")
        resume_meta = dict(chat.meta or {})
        if user_name:
            resume_meta["name"] = user_name
        if user_email:
            resume_meta["email"] = user_email
        resume_meta["widget_code"] = widget.code
        chat.meta = merge_chat_meta(
            resume_meta,
            request,
            {"source_page_url": source_page_url},
        )
        await request["db"].commit()
    else:
        chat = Chat(
            title=f"Chat for {user_name or user_uid}",
            user_uid=user_uid,
            meta=merge_chat_meta(
                {"name": user_name, "email": user_email, "widget_code": widget.code},
                request,
                {"source_page_url": source_page_url},
            ),
        )
        request["db"].add(chat)
        await request["db"].commit()

    payload = serializer.dumps([user_uid, chat.id, widget.code], salt="vchat")
    signed_chat_id = serializer.dumps(chat.id, salt="chat")
    support_csrf_token = request.app[SIGNER_KEY].dumps({"chat_id": chat.id})
    initial_messages = await _initial_messages_for_chat(
        request["db"],
        chat=chat,
        serializer=serializer,
    )

    project = _project_context(request)
    provider_obj, model_obj = resolve_ai_settings(project.provider, project.model)

    return {
        "project": project,
        "chat": chat,
        "payload": payload,
        "widget": widget,
        "ai_provider_options": get_ai_provider_options(),
        "current_ai_provider": provider_obj.id,
        "current_ai_model": model_obj.id,
        "current_ai_model_label": model_obj.label,
        "current_ai_provider_label": provider_obj.title,
        "allow_ai_switch": False,
        "ai_settings_url": None,
        "support_csrf_token": support_csrf_token,
        "signed_chat_id": signed_chat_id,
        "initial_messages": initial_messages,
    }


@meta(title="Виджет чата")
@aiohttp_jinja2.template("chat/chat.html")
async def public_widget_chat(request):
    code = request.match_info.get("code", "").strip()
    widget = await _widget_integration_by_code(request, code)
    return await _render_public_chat(request, widget)


@meta(title="Файлы")
@login_required()
@aiohttp_jinja2.template("projects/files.html")
async def project_files(request):
    db_session = request["db"]
    if request.method == "POST":
        data = await request.post()
        validate_signed_user_csrf(request, data.get("csrf_token") or None)
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
            raw_content=b"",
            raw_content_type="text/markdown",
            raw_content_size=0,
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
        await async_update_page_shingles(
            db_session,
            page_id=document.id,
            source_id=document.source_id,
            content=document.content,
        )
        await db_session.commit()
        await admin_event("file_create", request)
        location = request.app.router["file_document"].url_for(
            document_id=str(document.id)
        )
        raise web.HTTPFound(location=location)

    files_rows = await _files_rows(db_session)

    return {
        "active_section": str(request.app.router["project_files"].url_for()),
        "files_rows": files_rows,
        "current_document": None,
    }


@meta(title="Файлы")
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
        validate_signed_user_csrf(request, data.get("csrf_token") or None)
        action = str(data.get("action") or "save")
        if action == "delete":
            await db_session.delete(document)
            await db_session.commit()
            await admin_event("file_delete", request)
            raise web.HTTPFound(location=request.app.router["project_files"].url_for())

        content = str(data.get("content") or "")
        too_big = is_document_too_big(content)
        document.content = content
        document.hash_value = content
        document.length = len(content)
        document.status = PageStatus.ready if too_big else PageStatus.parsing
        document.status_error = PageStatusError.too_big if too_big else None
        document.patch_meta(
            remove=("error", "message", "reason", "exception_class"),
            **(
                {
                    "reason": PageStatusError.too_big.value,
                    "message": document_too_big_message(content),
                }
                if too_big
                else {}
            ),
        )
        document.updated_at = datetime.now(timezone.utc)
        await db_session.execute(sa.delete(Chunk).where(Chunk.page_id == document.id))
        await async_update_page_shingles(
            db_session,
            page_id=document.id,
            source_id=document.source_id,
            content=document.content,
        )
        await db_session.commit()
        if not too_big:
            schedule_index_document(document.id)
        await admin_event("file_update", request)
        await flash(request, "Файл сохранен", "success")
        raise web.HTTPFound(location=request.path)

    files_rows = await _files_rows(db_session)

    return {
        "active_section": str(request.app.router["project_files"].url_for()),
        "files_rows": files_rows,
        "current_document": document,
    }
