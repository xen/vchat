import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp_jinja2
import redis.asyncio as aioredis
import sqlalchemy as sa

from vchat.db import async_session_factory
from vchat.guardrails import mask_russian_pii
from vchat.i18n import _
from vchat.models import Chat, ChatMsg
from vchat.settings import config
from vchat.utils import login_required, meta

from .views import _project_context

logger = logging.getLogger("vchat.projects.chats")
REDIS_URL = config.get("redis_uri", "redis://localhost:6379/3")
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

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


@meta(title=_("Chats"))
@login_required()
@aiohttp_jinja2.template("projects/chats.html")
async def chats_list(request):
    active_chat_ids = await redis.smembers("active_chats")
    active_chats = []

    if active_chat_ids:
        stmt = sa.select(Chat).where(Chat.id.in_(list(active_chat_ids)))
        async with async_session_factory() as db:
            result = await db.execute(stmt)
            active_chats = result.scalars().all()

    return {
        "project": _project_context(request),
        "active_chats": active_chats,
    }


@meta(title=_("Chat History"))
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
    date_from_raw = request.query.get("date_from", "").strip()
    date_to_raw = request.query.get("date_to", "").strip()
    guardrail_reason = request.query.get("guardrail_reason", "").strip()
    valid_guardrail_reasons = {key for key, _ in GUARDRAIL_FILTER_OPTIONS}
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
        created_from is not None
        and created_to_exclusive is not None
        and created_from > created_to_exclusive
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

    search_tsquery = None
    chat_vector = None
    search_msg = None
    search_in_messages = None
    if search_query:
        search_tsquery = sa.func.websearch_to_tsquery("simple", search_query)
        chat_search_text = (
            sa.func.coalesce(Chat.title, "")
            + sa.literal(" ")
            + sa.func.coalesce(Chat.user_uid, "")
        )
        chat_vector = sa.func.to_tsvector("simple", chat_search_text)
        search_msg = sa.orm.aliased(ChatMsg)
        msg_vector = sa.func.to_tsvector("simple", sa.func.coalesce(search_msg.text, ""))
        search_in_messages = sa.exists(
            sa.select(sa.literal(1)).where(
                search_msg.chat_id == Chat.id,
                search_msg.role.in_(("user", "assistant")),
                msg_vector.op("@@")(search_tsquery),
            )
        )

    total_query = (
        sa.select(Chat.id)
        .outerjoin(
            ChatMsg, sa.and_(Chat.id == ChatMsg.chat_id, ChatMsg.role == "assistant")
        )
        .group_by(Chat.id)
    )

    if search_query:
        total_query = total_query.where(
            sa.or_(
                chat_vector.op("@@")(search_tsquery),
                search_in_messages,
            )
        )
    if guardrail_reason:
        reason_msg = sa.orm.aliased(ChatMsg)
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

    total = await request["db"].scalar(
        sa.select(sa.func.count()).select_from(total_query.subquery())
    ) or 0

    total_pages = (total + per_page - 1) // per_page if total else 0
    if total_pages and page > total_pages:
        page = total_pages
    if not total_pages:
        page = 1

    offset = (page - 1) * per_page if total else 0

    stmt = (
        sa.select(
            Chat,
            sa.func.count(sa.case((ChatMsg.vote.is_(True), 1))).label("upvotes"),
            sa.func.count(sa.case((ChatMsg.vote.is_(False), 1))).label("downvotes"),
            sa.func.count(guardrail_case).label("guardrail_hits"),
        )
        .outerjoin(
            ChatMsg, sa.and_(Chat.id == ChatMsg.chat_id, ChatMsg.role == "assistant")
        )
        .group_by(Chat.id)
        .order_by(Chat.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )

    if search_query:
        stmt = stmt.where(
            sa.or_(
                chat_vector.op("@@")(search_tsquery),
                search_in_messages,
            )
        )
    if guardrail_reason:
        reason_msg = sa.orm.aliased(ChatMsg)
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
        chats.append(chat)

    range_start = offset + 1 if chats else 0
    range_end = offset + len(chats) if chats else 0

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

    has_prev = total_pages > 0 and page > 1
    has_next = total_pages > 0 and page < total_pages

    if total_pages <= 7 and total_pages > 0:
        page_numbers = list(range(1, total_pages + 1))
    elif total_pages > 0:
        page_numbers = [1]
        if page - 2 > 2:
            page_numbers.append(None)
        for number in range(max(2, page - 2), min(total_pages - 1, page + 2) + 1):
            page_numbers.append(number)
        if total_pages - (page + 2) > 1:
            page_numbers.append(None)
        if total_pages > 1:
            page_numbers.append(total_pages)
    else:
        page_numbers = []

    pagination_pages: list[dict] = []
    for number in page_numbers:
        if number is None:
            pagination_pages.append({"number": None})
            continue
        pagination_pages.append(
            {
                "number": number,
                "is_current": number == page,
                "query": _query_for_page(number),
            }
        )

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_query": _query_for_page(page - 1) if has_prev else None,
        "next_query": _query_for_page(page + 1) if has_next else None,
        "pages": pagination_pages,
        "range_start": range_start,
        "range_end": range_end,
    }

    return {
        "project": _project_context(request),
        "chats": chats,
        "pagination": pagination,
        "guardrail_filter": guardrail_filter,
        "guardrail_reason": guardrail_reason,
        "guardrail_filter_options": GUARDRAIL_FILTER_OPTIONS,
        "search_query": search_query,
        "date_from": date_from_value,
        "date_to": date_to_value,
    }


@meta(title=_("Chat History"))
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

    def _legacy_guardrail_info(full_context: str | None) -> tuple[str | None, list[str]]:
        if not full_context or not full_context.startswith("guardrail_blocked"):
            return None, []
        marker, _, reasons_part = full_context.partition("|")
        stage = marker.removeprefix("guardrail_blocked_").strip() or None
        reasons = []
        if reasons_part:
            reasons = [r.strip() for r in reasons_part.split(",") if r.strip()]
        return stage, reasons

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

        legacy_stage, legacy_reasons = _legacy_guardrail_info(msg.full_context)
        reasons = []
        if msg.guardrail_reasons:
            reasons.extend(msg.guardrail_reasons)
        if legacy_reasons:
            reasons.extend(legacy_reasons)
        seen = set()
        unique_reasons = []
        for reason in reasons:
            if reason and reason not in seen:
                seen.add(reason)
                unique_reasons.append(reason)

        stage = msg.guardrail_stage or legacy_stage
        msg.guardrail_hit = bool(msg.guardrail_triggered or legacy_reasons)
        msg.guardrail_stage_display = GUARDRAIL_STAGE_LABELS.get(stage, stage)
        msg.guardrail_rules = [
            GUARDRAIL_REASON_LABELS.get(rule, rule) for rule in unique_reasons
        ]

    return {
        "project": _project_context(request),
        "chat": chat,
        "messages": messages,
    }
