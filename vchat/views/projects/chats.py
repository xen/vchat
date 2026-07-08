import logging
import re
import json
import html
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp_jinja2
import markdown
import redis.asyncio as aioredis
import sqlalchemy as sa
from aiohttp import web
from sqlalchemy.orm import aliased

from vchat.db import async_session_factory
from vchat.views.chat.ai import suggested_actions_from_payload
from vchat.views.chat.guardrails import mask_russian_pii
from vchat.views.chat.sources import enrich_source_payloads
from vchat.models import Chat, ChatMsg, Page
from vchat.settings import cfg
from vchat.utils import login_required, meta, paginator

logger = logging.getLogger("vchat.projects.chats")
redis = aioredis.from_url(cfg.redis_uri, decode_responses=True)

GUARDRAIL_REASON_LABELS = {
    "russian_pii": "Персональные данные (РФ)",
    "phone_number_ru": "Телефон РФ",
    "passport_ru": "Паспорт РФ",
    "inn_ru": "ИНН",
    "snils_ru": "СНИЛС",
    "oms_ru": "Полис ОМС",
    "input_blocked": "Блокировка входного сообщения",
    "output_blocked": "Блокировка ответа",
    "guardrail_tripwire": "Tripwire guardrail",
    "provider_block": "Блокировка провайдера",
    "content_filter": "Контент-фильтр",
    "refusal": "Refusal",
}

GUARDRAIL_STAGE_LABELS = {
    "input": "На входе",
    "output": "На выходе",
    "stream": "Во время генерации",
}

GUARDRAIL_FILTER_OPTIONS: list[tuple[str, str]] = [
    ("phone_number_ru", "Телефон РФ"),
    ("passport_ru", "Паспорт РФ"),
    ("inn_ru", "ИНН"),
    ("snils_ru", "СНИЛС"),
    ("oms_ru", "Полис ОМС"),
    ("content_filter", "Контент-фильтр"),
    ("refusal", "Refusal"),
    ("provider_block", "Блокировка провайдера"),
    ("guardrail_tripwire", "Tripwire guardrail"),
    ("input_blocked", "Блокировка входного сообщения"),
    ("output_blocked", "Блокировка ответа"),
]
HISTORY_FIRST_MESSAGE_PREVIEW_LENGTH = 150

HISTORY_MARKDOWN_TAGS = {"p", "ul", "ol", "li", "strong", "em", "code", "pre"}
HISTORY_MARKDOWN_VOID_TAGS = {"br"}
HISTORY_MARKDOWN_LINK_PROTOCOLS = {"http", "https", "mailto"}


class _HistoryMarkdownAllowlistParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HISTORY_MARKDOWN_VOID_TAGS:
            self.parts.append(f"<{tag}>")
            return
        if tag == "a":
            href = ""
            for name, value in attrs:
                if name == "href" and value:
                    href = value.strip()
                    break
            parsed = urlparse(href)
            if parsed.scheme not in HISTORY_MARKDOWN_LINK_PROTOCOLS:
                return
            escaped_href = html.escape(href, quote=True)
            self.parts.append(
                f'<a href="{escaped_href}" target="_blank" rel="noopener noreferrer" class="link link-primary">'
            )
            self.open_tags.append(tag)
            return
        if tag in HISTORY_MARKDOWN_TAGS:
            self.parts.append(f"<{tag}>")
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" and tag not in HISTORY_MARKDOWN_TAGS:
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def sanitized(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


def _render_history_markdown(value: str | None) -> str:
    escaped_markdown = html.escape(value or "", quote=False)
    rendered = markdown.markdown(
        escaped_markdown,
        extensions=["fenced_code"],
        output_format="html",
    )
    parser = _HistoryMarkdownAllowlistParser()
    parser.feed(rendered)
    parser.close()
    return parser.sanitized()


def _history_message_sources(row: ChatMsg) -> list[dict[str, object]]:
    payload = None
    if row.full_context:
        try:
            payload = json.loads(row.full_context)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, dict) and isinstance(payload.get("sources"), list):
        raw_items = payload["sources"]
    else:
        raw_items = getattr(row, "used_chunks", None) or []

    sources: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        page_url = item.get("page_url") or item.get("uri")
        title = item.get("title")
        display_path = item.get("display_path") or title
        key = (
            item.get("citation_id"),
            page_url,
            display_path,
            item.get("section_path"),
            item.get("header_text"),
            item.get("kind"),
        )
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "citation_id": item.get("citation_id"),
                "uri": item.get("uri"),
                "page_url": page_url,
                "title": title,
                "source_title": item.get("source_title"),
                "display_path": display_path,
                "summary": item.get("summary"),
                "kind": item.get("kind"),
                "header_text": item.get("header_text"),
                "section_path": item.get("section_path"),
            }
        )
    return sources


def _history_message_suggestions(row: ChatMsg) -> tuple[list[str], dict | None]:
    if not row.full_context:
        return [], None
    try:
        payload = json.loads(row.full_context)
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], None
    if not isinstance(payload, dict):
        return [], None

    selected = payload.get("selected_suggested_action")
    return suggested_actions_from_payload(payload.get("suggested_actions")), (
        selected if isinstance(selected, dict) else None
    )


def _history_message_suggestions_error(row: ChatMsg) -> dict | None:
    if not row.full_context:
        return None
    try:
        payload = json.loads(row.full_context)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    error = payload.get("suggested_actions_error")
    return error if isinstance(error, dict) else None


def _mark_inferred_selected_suggestions(messages: list[ChatMsg]) -> None:
    next_user_text: str | None = None
    for msg in reversed(messages):
        if msg.role == "user":
            next_user_text = (msg.text or "").strip()
            continue
        if (
            msg.role != "assistant"
            or msg.selected_suggested_action
            or not msg.suggested_actions
            or not next_user_text
        ):
            continue
        if next_user_text in msg.suggested_actions:
            msg.selected_suggested_action = {"text": next_user_text}


def _history_chat_user_label(chat: Chat) -> str:
    meta = chat.meta if isinstance(chat.meta, dict) else {}
    name = (meta.get("name") or "").strip()
    email = (meta.get("email") or "").strip()
    if name and email:
        return f"{name} ({email})"
    return name or email or chat.user_uid


def _history_first_message_preview(text: str | None) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) <= HISTORY_FIRST_MESSAGE_PREVIEW_LENGTH:
        return value
    return f"{value[:HISTORY_FIRST_MESSAGE_PREVIEW_LENGTH].rstrip()}..."


async def _mark_deleted_history_sources(db, messages: list[ChatMsg]) -> None:
    page_urls = {
        source["page_url"]
        for msg in messages
        for source in getattr(msg, "context_sources", [])
        if source.get("page_url")
    }
    if not page_urls:
        return

    existing_rows = await db.execute(sa.select(Page.uri).where(Page.uri.in_(page_urls)))
    existing_urls = {row.uri for row in existing_rows}
    for msg in messages:
        for source in getattr(msg, "context_sources", []):
            page_url = source.get("page_url")
            source["page_deleted"] = bool(page_url) and page_url not in existing_urls


@meta(title="Чаты")
@login_required()
@aiohttp_jinja2.template("projects/chats.html")
async def chats_list(_ignore_request):
    active_chat_ids = await redis.smembers("active_chats")
    active_chats = []

    if active_chat_ids:
        stmt = sa.select(Chat).where(Chat.id.in_(list(active_chat_ids)))
        async with async_session_factory() as db:
            result = await db.execute(stmt)
            active_chats = result.scalars().all()

    return {
        "active_chats": active_chats,
    }


@meta(title="История чатов")
@login_required()
@aiohttp_jinja2.template("projects/history.html")
async def history_list(request):
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    per_page = 20
    search_query = request.query.get("search", "").strip()
    legacy_identity_query = request.query.get("fingerprint", "").strip()
    if not search_query and legacy_identity_query:
        search_query = legacy_identity_query
    date_from_raw = request.query.get("date_from", "").strip()
    date_to_raw = request.query.get("date_to", "").strip()
    guardrail_reason = request.query.get("guardrail_reason", "").strip()
    valid_guardrail_reasons = {item[0] for item in GUARDRAIL_FILTER_OPTIONS}
    if guardrail_reason not in valid_guardrail_reasons:
        guardrail_reason = ""
    guardrail_filter = request.query.get("guardrail", "").strip() == "1" or bool(
        guardrail_reason
    )

    def _month_end(year: int, month: int) -> int:
        if month == 12:
            return 31
        first_next_month = datetime(year, month + 1, 1)
        return (first_next_month - timedelta(days=1)).day

    def _parse_date_range(value: str) -> tuple[datetime, datetime, str] | None:
        if not value:
            return None
        raw = value.strip()
        if not raw:
            return None

        if re.fullmatch(r"\d{4}", raw):
            year = int(raw)
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            return start, end, f"{year:04d}"

        if re.fullmatch(r"\d{4}/\d{1,2}", raw):
            year_s, month_s = raw.split("/")
            year = int(year_s)
            month = int(month_s)
            if not (1 <= month <= 12):
                return None
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            if month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            return start, end, f"{year:04d}/{month:02d}"

        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw):
            year_s, month_s, day_s = raw.split("/")
            year = int(year_s)
            month = int(month_s)
            day = int(day_s)
            if not (1 <= month <= 12):
                return None
            max_day = _month_end(year, month)
            if not (1 <= day <= max_day):
                return None
            start = datetime(year, month, day, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            return start, end, f"{year:04d}/{month:02d}/{day:02d}"

        return None

    parsed_from = _parse_date_range(date_from_raw)
    parsed_to = _parse_date_range(date_to_raw)

    created_from = parsed_from[0] if parsed_from else None
    created_to_exclusive = parsed_to[1] if parsed_to else None
    date_from_value = parsed_from[2] if parsed_from else date_from_raw
    date_to_value = parsed_to[2] if parsed_to else date_to_raw

    if (
        parsed_from is not None
        and parsed_to is not None
        and parsed_from[0] > parsed_to[1]
    ):
        created_from, created_to_exclusive = parsed_to[0], parsed_from[1]
        date_from_value, date_to_value = date_to_value, date_from_value
    guardrail_case = sa.case(
        (
            sa.or_(
                ChatMsg.guardrail_triggered.is_(True),
                ChatMsg.full_context.like("guardrail_blocked%"),
            ),
            1,
        )
    )

    search_filter = None
    if search_query:
        search_tsquery = sa.func.websearch_to_tsquery("simple", search_query)
        identity_like = f"%{search_query}%"
        chat_search_text = (
            sa.func.coalesce(Chat.title, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.user_uid, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.meta["device_fingerprint"].astext, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.meta["ip_address"].astext, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.meta["name"].astext, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.meta["email"].astext, "")
        )
        chat_vector = sa.func.to_tsvector("simple", chat_search_text)
        search_msg = aliased(ChatMsg)
        msg_search_text = (
            sa.func.coalesce(search_msg.text, "")
            + sa.literal(" ")
            + sa.func.coalesce(search_msg.full_context, "")
        )
        msg_vector = sa.func.to_tsvector(
            "simple", msg_search_text
        )
        search_in_messages = sa.exists(
            sa.select(sa.literal(1)).where(
                search_msg.chat_id == Chat.id,
                search_msg.role.in_(("user", "assistant")),
                msg_vector.op("@@")(search_tsquery),
            )
        )
        search_filter = sa.or_(
            chat_vector.op("@@")(search_tsquery),
            Chat.title.ilike(identity_like),
            Chat.user_uid.ilike(identity_like),
            Chat.meta["device_fingerprint"].astext.ilike(identity_like),
            Chat.meta["ip_address"].astext.ilike(identity_like),
            Chat.meta["name"].astext.ilike(identity_like),
            Chat.meta["email"].astext.ilike(identity_like),
            search_in_messages,
        )

    total_query = (
        sa.select(Chat.id)
        .outerjoin(
            ChatMsg, sa.and_(Chat.id == ChatMsg.chat_id, ChatMsg.role == "assistant")
        )
        .group_by(Chat.id)
    )

    if search_filter is not None:
        total_query = total_query.where(search_filter)
    if guardrail_reason:
        reason_msg = aliased(ChatMsg)
        reason_filter = sa.exists(
            sa.select(sa.literal(1)).where(
                reason_msg.chat_id == Chat.id,
                sa.or_(
                    reason_msg.guardrail_reasons.op("@>")(
                        sa.cast([guardrail_reason], sa.ARRAY(sa.String()))
                    ),
                    reason_msg.full_context.like(f"%|%{guardrail_reason}%"),
                ),
            )
        )
        total_query = total_query.where(reason_filter)
    if created_from is not None:
        total_query = total_query.where(Chat.created_at >= created_from)
    if created_to_exclusive is not None:
        total_query = total_query.where(Chat.created_at < created_to_exclusive)
    if guardrail_filter:
        total_query = total_query.having(sa.func.count(guardrail_case) > 0)

    total = (
        await request["db"].scalar(
            sa.select(sa.func.count()).select_from(total_query.subquery())
        )
        or 0
    )

    page = paginator(total, page=page, per_page=per_page)["page"]

    offset = (page - 1) * per_page if total else 0

    aggregate_msg = aliased(ChatMsg)
    message_count = (
        sa.select(sa.func.count(aggregate_msg.id))
        .where(aggregate_msg.chat_id == Chat.id)
        .scalar_subquery()
    )
    token_count = (
        sa.select(sa.func.coalesce(sa.func.sum(aggregate_msg.tokens), 0))
        .where(aggregate_msg.chat_id == Chat.id)
        .scalar_subquery()
    )
    first_user_msg = aliased(ChatMsg)
    first_user_message = (
        sa.select(first_user_msg.text)
        .where(
            first_user_msg.chat_id == Chat.id,
            first_user_msg.role == "user",
        )
        .order_by(first_user_msg.created_at.asc(), first_user_msg.id.asc())
        .limit(1)
        .scalar_subquery()
    )

    stmt = (
        sa.select(
            Chat,
            sa.func.count(sa.case((ChatMsg.vote.is_(True), 1))).label("upvotes"),
            sa.func.count(sa.case((ChatMsg.vote.is_(False), 1))).label("downvotes"),
            sa.func.count(guardrail_case).label("guardrail_hits"),
            message_count.label("message_count"),
            token_count.label("token_count"),
            first_user_message.label("first_user_message"),
        )
        .outerjoin(
            ChatMsg, sa.and_(Chat.id == ChatMsg.chat_id, ChatMsg.role == "assistant")
        )
        .group_by(Chat.id)
        .order_by(Chat.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )

    if search_filter is not None:
        stmt = stmt.where(search_filter)
    if guardrail_reason:
        reason_msg = aliased(ChatMsg)
        reason_filter = sa.exists(
            sa.select(sa.literal(1)).where(
                reason_msg.chat_id == Chat.id,
                sa.or_(
                    reason_msg.guardrail_reasons.op("@>")(
                        sa.cast([guardrail_reason], sa.ARRAY(sa.String()))
                    ),
                    reason_msg.full_context.like(f"%|%{guardrail_reason}%"),
                ),
            )
        )
        stmt = stmt.where(reason_filter)
    if created_from is not None:
        stmt = stmt.where(Chat.created_at >= created_from)
    if created_to_exclusive is not None:
        stmt = stmt.where(Chat.created_at < created_to_exclusive)
    if guardrail_filter:
        stmt = stmt.having(sa.func.count(guardrail_case) > 0)
    result = await request["db"].execute(stmt)
    rows = result.all()

    chats = []
    for row in rows:
        chat = row.Chat
        chat.upvotes = row.upvotes
        chat.downvotes = row.downvotes
        chat.guardrail_triggered = (row.guardrail_hits or 0) > 0
        chat.message_count = row.message_count or 0
        chat.token_count = row.token_count or 0
        chat.browser = (chat.meta or {}).get("browser")
        chat.device_type = (chat.meta or {}).get("device_type")
        chat.device_fingerprint = (chat.meta or {}).get("device_fingerprint")
        chat.ip_address = (chat.meta or {}).get("ip_address")
        chat.user_label = _history_chat_user_label(chat)
        chat.first_user_message_preview = _history_first_message_preview(
            row.first_user_message
        )
        chats.append(chat)

    base_filters: dict[str, str] = {}
    if search_query:
        base_filters["search"] = search_query
    if date_from_value:
        base_filters["date_from"] = date_from_value
    if date_to_value:
        base_filters["date_to"] = date_to_value
    if guardrail_reason:
        base_filters["guardrail_reason"] = guardrail_reason

    def _query_for_page(target_page: int):
        query = {}
        if target_page > 1:
            query["page"] = str(target_page)
        if guardrail_filter:
            query["guardrail"] = "1"
        query.update(base_filters)
        return query or None

    def _href_for_page(target_page: int) -> str:
        query = _query_for_page(target_page)
        if query:
            return str(
                request.app.router["project_history"].url_for().with_query(query)
            )
        return str(request.app.router["project_history"].url_for())

    pagination = paginator(
        total,
        page=page,
        per_page=per_page,
        query_factory=_query_for_page,
        href_factory=_href_for_page,
    )

    return {
        "chats": chats,
        "pagination": pagination,
        "guardrail_filter": guardrail_filter,
        "guardrail_reason": guardrail_reason,
        "guardrail_filter_options": GUARDRAIL_FILTER_OPTIONS,
        "search_query": search_query,
        "date_from": date_from_value,
        "date_to": date_to_value,
    }


@meta(title="История чатов")
@login_required()
@aiohttp_jinja2.template("projects/history_detail.html")
async def history_detail(request):
    chat_id = request.match_info["chat_id"]

    chat = await request["db"].scalar(sa.select(Chat).where(Chat.id == chat_id))
    if not chat:
        raise web.HTTPNotFound()

    stmt = (
        sa.select(ChatMsg)
        .where(ChatMsg.chat_id == chat.id)
        .order_by(ChatMsg.created_at.asc())
    )
    result = await request["db"].execute(stmt)
    messages = result.scalars().all()
    chat_meta = chat.meta if isinstance(chat.meta, dict) else {}

    for msg in messages:
        masked_text = msg.text
        has_pii = False
        if msg.role == "user":
            masked_text, has_pii = mask_russian_pii(msg.text or "")
        msg.has_masked_pii = has_pii
        msg.text_segments = None
        if has_pii:
            parts = masked_text.split("***")
            segments = []
            for idx, part in enumerate(parts):
                if part:
                    segments.append({"masked": False, "text": part})
                if idx < len(parts) - 1:
                    segments.append({"masked": True, "text": "***"})
            msg.text_segments = segments
            msg.text_display = masked_text
        else:
            msg.text_display = msg.text
            msg.text_html = _render_history_markdown(msg.text_display)

        reasons = []
        if msg.guardrail_reasons:
            reasons.extend(msg.guardrail_reasons)
        seen = set()
        unique_reasons = []
        for reason in reasons:
            if reason and reason not in seen:
                seen.add(reason)
                unique_reasons.append(reason)

        stage = msg.guardrail_stage
        msg.guardrail_hit = bool(msg.guardrail_triggered)
        msg.guardrail_stage_display = GUARDRAIL_STAGE_LABELS.get(stage, stage)
        msg.guardrail_rules = [
            GUARDRAIL_REASON_LABELS.get(rule, rule) for rule in unique_reasons
        ]
        msg.suggested_actions = []
        msg.selected_suggested_action = None
        msg.suggested_actions_error = None
        msg.context_sources = _history_message_sources(msg)
        msg.is_error = False
        msg.error_kind = None
        msg.request_id = None
        if msg.context_sources:
            msg.context_sources = await enrich_source_payloads(
                request["db"],
                msg.context_sources,
            )
        msg.reason_code = None
        if msg.role == "assistant" and msg.full_context:
            try:
                payload = json.loads(msg.full_context)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                policy = (
                    payload.get("policy")
                    if isinstance(payload.get("policy"), dict)
                    else {}
                )
                msg.reason_code = policy.get("reason_code")
                msg.error_kind = payload.get("error")
                msg.request_id = payload.get("request_id")
                msg.is_error = bool(msg.error_kind)
                msg.suggested_actions, msg.selected_suggested_action = (
                    _history_message_suggestions(msg)
                )
                msg.suggested_actions_error = _history_message_suggestions_error(msg)

    await _mark_deleted_history_sources(request["db"], messages)
    _mark_inferred_selected_suggestions(messages)

    return {
        "chat": chat,
        "chat_meta": chat_meta,
        "messages": messages,
    }
