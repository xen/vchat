import re
import unicodedata

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from anyascii import anyascii
from slugify import slugify

from vchat.models.support import Request
from vchat.utils import login_required
from vchat.text import _

__all__ = [
    "admin_request_all",
    "admin_request_detail",
]


def translit_slug(text: str, *, max_length: int = 120) -> str:
    """
    Делает удобный ASCII-slug из текста на любом языке:
    1) Unicode нормализация
    2) best-effort транслитерация (anyascii)
    3) slugify (дефисы, нижний регистр, чистка)
    """
    if text is None:
        raise TypeError("text must be a str, not None")
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text)!r}")

    # 1) нормализуем
    normalized = unicodedata.normalize("NFKC", text).strip()

    # 2) транслитерация/ASCII-fallback
    ascii_text = anyascii(normalized)

    # 3) slugify
    s = slugify(
        ascii_text,
        lowercase=True,
        max_length=max_length,
        separator="-",
    )

    if not s:
        fallback = re.sub(r"\W+", "-", normalized, flags=re.UNICODE).strip("-").lower()
        s = fallback[:max_length] or "item"

    return s


@login_required()
@aiohttp_jinja2.template("support/admin/request_list.html")
async def admin_request_all(request):
    db = request["db"]

    # Filter status
    status = request.query.get("status", "open")  # open, closed, all

    stmt = sa.select(Request).order_by(Request.created_at.desc())
    if status != "all":
        if status == "closed":
            stmt = stmt.where(Request.status == "closed")
        else:  # open or any other
            stmt = stmt.where(Request.status != "closed")

    result = await db.execute(stmt)
    tickets = result.scalars().all()

    return {"tickets": tickets, "current_status": status}


@login_required()
@aiohttp_jinja2.template("support/admin/request_detail.html")
async def admin_request_detail(request):
    db = request["db"]
    request_id = int(request.match_info["request_id"])

    # Fetch request
    request_obj = await db.scalar(
        sa.select(Request)
        .options(sa.orm.joinedload(Request.user))
        .where(Request.id == request_id)
    )
    if not request_obj:
        raise web.HTTPNotFound()

    return {"request": request_obj}


@login_required()
async def support_actions(request):
    db = request["db"]
    user = request["user"]
    data = await request.post()
    action = data.get("action")

    if action == "close_request":
        tid = int(data.get("id"))
        await db.execute(
            sa.update(Request)
            .values(status="closed", updated_at=sa.func.now())
            .where(Request.id == tid)
        )
        await db.commit()
        return web.Response(text=str(_("Closed")))

    elif action == "reopen_request":
        tid = int(data.get("id"))
        await db.execute(
            sa.update(Request)
            .values(status="open", updated_at=sa.func.now())
            .where(Request.id == tid)
        )
        await db.commit()
        return web.Response(text=str(_("Reopened")))
