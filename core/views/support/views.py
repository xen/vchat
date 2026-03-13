import re
import unicodedata

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from anyascii import anyascii
from slugify import slugify

from core.models.support import Chapter, Page, Ticket, TicketComment
from core.utils import flash, login_required, json
from core.settings import config
from core.i18n import lazy_gettext as _

__all__ = [
    "admin_chapters",
    "admin_pages",
    "admin_page_edit",
    "get_chapters_api",
    "admin_favorites",
    "admin_tickets",
    "admin_ticket_detail",
    "support_actions",
    "user_tickets",
    "user_ticket_detail",
    "support_root",
    "public_support",
    "public_article",
    "translit_slug",
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
@aiohttp_jinja2.template("support/admin/chapters.html")
async def admin_chapters(request):
    db = request["db"]
    user = request["user"]

    # Determine supported languages
    supported = config.get("lang_supported", {"en": "English"})
    default_lang = "en"
    if user.language in supported and user.language != "en":
        default_lang = user.language
    elif "en" not in supported:
        default_lang = list(supported.keys())[0]

    current_lang = request.query.get("lang", default_lang)

    stmt = (
        sa.select(Chapter).where(Chapter.lang == current_lang).order_by(Chapter.order)
    )
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    return {
        "chapters": chapters,
        "current_lang": current_lang,
        "langs": supported,
    }


@login_required()
@aiohttp_jinja2.template("support/admin/pages.html")
async def admin_pages(request):
    db = request["db"]
    user = request["user"]

    # Lang logic
    supported = config.get("lang_supported", {"en": "English"})
    default_lang = "en"
    if user.language in supported and user.language != "en":
        default_lang = user.language
    elif "en" not in supported:
        default_lang = list(supported.keys())[0]

    current_lang = request.query.get("lang", default_lang)
    chapter_id = request.query.get("chapter_id")

    # Fetch chapters for filter
    chapters_stmt = (
        sa.select(Chapter).where(Chapter.lang == current_lang).order_by(Chapter.order)
    )
    chapters = (await db.execute(chapters_stmt)).scalars().all()

    # Query Pages
    query = (
        sa.select(Page)
        .where(Page.lang == current_lang)
        .order_by(Page.chapter_id, Page.order)
    )

    if chapter_id:
        query = query.where(Page.chapter_id == int(chapter_id))

    result = await db.execute(query)
    pages = result.scalars().all()

    return {
        "pages": pages,
        "current_lang": current_lang,
        "langs": supported,
        "chapters": chapters,
        "current_chapter_id": int(chapter_id) if chapter_id else None,
    }


@login_required()
@aiohttp_jinja2.template("support/admin/page_edit.html")
async def admin_page_edit(request):
    db = request["db"]
    pid = request.query.get("id")
    # Default lang from query or default to 'en'
    current_lang = request.query.get("lang", "en")
    page = None

    if pid:
        page = await db.get(Page, int(pid))
        if page:
            current_lang = page.lang  # Use page's language if editing

    # Fetch chapters for the current language
    stmt = (
        sa.select(Chapter).where(Chapter.lang == current_lang).order_by(Chapter.order)
    )
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    # Determine validation for languages?
    supported_langs = config.get("lang_supported", {"en": "English"})

    return {
        "page": page,
        "chapters": chapters,
        "lang": current_lang,
        "langs": supported_langs,
    }


@login_required()
async def get_chapters_api(request):
    db = request["db"]
    lang = request.query.get("lang", "en")

    stmt = sa.select(Chapter).where(Chapter.lang == lang).order_by(Chapter.order)
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    data = []
    for ch in chapters:
        title = ch.title
        if ch.is_hidden:
            title = f"{title} ({_('Hidden')})"
        data.append({"id": ch.id, "title": title})

    return web.json_response(data)


@login_required()
@aiohttp_jinja2.template("support/admin/favorites.html")
async def admin_favorites(request):
    db = request["db"]
    user = request["user"]

    # Lang logic
    supported = config.get("lang_supported", {"en": "English"})
    default_lang = "en"
    if user.language in supported and user.language != "en":
        default_lang = user.language
    elif "en" not in supported:
        default_lang = list(supported.keys())[0]

    current_lang = request.query.get("lang", default_lang)

    stmt = (
        sa.select(Page)
        .where(Page.is_favorite == True, Page.lang == current_lang)
        .order_by(Page.favorit_order)
    )
    pages = (await db.execute(stmt)).scalars().all()

    return {
        "pages": pages,
        "current_lang": current_lang,
        "langs": supported,
    }


@login_required()
@aiohttp_jinja2.template("support/admin/ticket_list.html")
async def admin_tickets(request):
    db = request["db"]

    # Filter status
    status = request.query.get("status", "open")  # open, closed, all

    stmt = sa.select(Ticket).order_by(Ticket.created_at.desc())
    if status != "all":
        if status == "closed":
            stmt = stmt.where(Ticket.status == "closed")
        else:  # open or any other
            stmt = stmt.where(Ticket.status != "closed")

    # Join user to show who created?
    stmt = stmt.options(sa.orm.joinedload(Ticket.user))

    result = await db.execute(stmt)
    tickets = result.scalars().all()

    return {"tickets": tickets, "current_status": status}


@login_required()
@aiohttp_jinja2.template("support/admin/ticket_detail.html")
async def admin_ticket_detail(request):
    db = request["db"]
    ticket_id = int(request.match_info["ticket_id"])

    # Fetch ticket
    ticket = await db.scalar(
        sa.select(Ticket)
        .options(sa.orm.joinedload(Ticket.user))
        .where(Ticket.id == ticket_id)
    )
    if not ticket:
        raise web.HTTPNotFound()

    # Fetch comments
    comments_stmt = (
        sa.select(TicketComment)
        .options(sa.orm.joinedload(TicketComment.user))
        .where(TicketComment.ticket_id == ticket_id)
        .order_by(TicketComment.created_at.asc())
    )
    comments = (await db.execute(comments_stmt)).scalars().all()

    return {"ticket": ticket, "comments": comments}


@login_required()
async def support_actions(request):
    db = request["db"]
    user = request["user"]
    data = await request.post()
    action = data.get("action")

    if action == "update_chapter_order":
        ids = json.loads(data.get("ids", "[]"))
        lang = data.get("lang")

        # 1. Update order for current lang
        # We need to fetch all these chapters to verify they belong to lang?
        # Or just bulk update.
        # But we also need to update other languages using from_id reference.

        # Fetch current chapters map map[id] -> from_id
        stmt = sa.select(Chapter.id, Chapter.from_id).where(
            Chapter.id.in_(map(int, ids))
        )
        res = await db.execute(stmt)
        chapter_map = dict(res.all())  # id -> from_id

        # We assign order based on list index.
        # order is 0-indexed.

        # Logic:
        # For each passed ID in list:
        #   Update that ID order = index.
        #   Also find all chapters where from_id == (chapter_map[ID].from_id OR ID if from_id is None)
        #   Wait, if we are sorting English (source), then other langs link to it via from_id.
        #   If we are sorting Translation, we should probably check if we are allowed to sort translations independently?
        #   Requirement: "То же order надо прописать для каждого языка из доступных используя from_id в качестве референса."
        #   This implies strict sync.

        # Let's assume we sync based on "Identity Group".
        # Identity Group is defined by the source chapter ID.
        # If chapter is source (from_id is None), Identity is ID.
        # If chapter is translation, Identity is from_id.

        # We iterate through the new order of IDs.
        for idx, cid in enumerate(ids):
            cid = int(cid)
            # update current
            await db.execute(
                sa.update(Chapter).values(order=idx).where(Chapter.id == cid)
            )

            # Identify source ID
            source_id = chapter_map.get(cid)  # from_id
            if source_id is None:
                source_id = cid  # It is source

            # Update all siblings (same source_id)
            # siblings = where form_id == source_id OR id == source_id
            await db.execute(
                sa.update(Chapter)
                .values(order=idx)
                .where(sa.or_(Chapter.from_id == source_id, Chapter.id == source_id))
            )

        await db.commit()
        await flash(request, _("Chapter order updated"))
        return web.json_response({"status": "ok"})

    elif action == "sync_chapters":
        from jobs.celery import app as celery_app

        celery_app.send_task("jobs.content.translate_chapters")
        await flash(request, _("Chapters sync started"))
        return web.json_response({"status": "ok"})

    elif action == "add_chapter":
        title = data.get("title")
        lang = data.get("lang", "en")
        slug = translit_slug(title)

        # Check uniqueness of slug if crucial, but assuming unique constraint handling or loose?
        # Models don't have unique on slug strictly visible in snippet, but likely should.

        new_chapter = Chapter(
            title=title,
            lang=lang,
            slug=slug,
            order=1000,  # default to end
            is_translated=False,
            is_hidden=False,
        )
        db.add(new_chapter)
        await db.commit()

        # Trigger translation job
        from jobs.celery import app as celery_app

        celery_app.send_task("jobs.content.translate_chapters")

        await flash(request, _("Chapter added"))
        return web.json_response({"status": "ok"})

    elif action == "edit_chapter":
        cid = int(data.get("id"))
        title = data.get("title")
        slug = data.get("slug")
        if not slug:
            slug = translit_slug(title)

        await db.execute(
            sa.update(Chapter).values(title=title, slug=slug).where(Chapter.id == cid)
        )
        await db.commit()
        await flash(request, _("Chapter updated"))
        return web.json_response({"status": "ok"})

    elif action == "toggle_hidden":
        cid = int(data.get("id"))
        # Toggle
        chapter = await db.scalar(sa.select(Chapter).where(Chapter.id == cid))
        if chapter:
            new_hidden_state = not chapter.is_hidden
            chapter.is_hidden = new_hidden_state

            # Sync to all languages
            source_id = chapter.from_id if chapter.from_id else chapter.id

            await db.execute(
                sa.update(Chapter)
                .values(is_hidden=new_hidden_state)
                .where(sa.or_(Chapter.id == source_id, Chapter.from_id == source_id))
            )

            await db.commit()

            from aiohttp_jinja2 import render_string

            html = render_string(
                "support/admin/_chapter_row.html",
                request,
                {"chapter": chapter},
            )
            await flash(request, _("Visibility toggled"))
            return web.Response(text=html, content_type="text/html")
        return web.Response(status=404)

    elif action == "save_page":
        import datetime

        pid = data.get("id")
        lang = data.get("lang")
        title = data.get("title")
        slug_input = data.get("slug", "").strip()
        chapter_id = int(data.get("chapter_id"))
        body = data.get("body")
        is_hidden = data.get("is_hidden") == "on"
        is_favorite = (
            data.get("is_favorite") == "on"
        )  # Updated to look for 'on' from toggle

        # Translit body for HTML (existing logic)
        import markdown

        body_html = markdown.markdown(body)

        # Slug Logic
        if not slug_input:
            # Generate
            slug = translit_slug(title)
            # Check collision
            exists = await db.scalar(
                sa.select(Page.id).where(
                    sa.and_(
                        Page.chapter_id == chapter_id,
                        Page.slug == slug,
                        Page.id != (int(pid) if pid else -1),
                    )
                )
            )
            if exists:
                slug = f"{slug}_{datetime.date.today()}"
        else:
            slug = translit_slug(slug_input)
            # Check collision
            exists = await db.scalar(
                sa.select(Page.id).where(
                    sa.and_(
                        Page.chapter_id == chapter_id,
                        Page.slug == slug,
                        Page.id != (int(pid) if pid else -1),
                    )
                )
            )
            if exists:
                await flash(
                    request, _("Error: Slug already exists in this chapter"), "error"
                )
                return web.HTTPFound(
                    location=request.headers.get("Referer", "/admin/support")
                )

        if pid:
            # Update
            pid = int(pid)
            await db.execute(
                sa.update(Page)
                .values(
                    lang=lang,
                    title=title,
                    slug=slug,
                    body=body,
                    body_html=body_html,
                    is_hidden=is_hidden,
                    is_favorite=is_favorite,
                    chapter_id=chapter_id,
                )
                .where(Page.id == pid)
            )
            await db.commit()

            # Sync translations? Or just current?
            # User requirement: "Sync translations which runs background task..."
            # Usually separate button. Here just save.

            await flash(request, _("Page updated"))
            return web.HTTPFound(
                location=request.headers.get("Referer", "/admin/support")
            )

        else:
            # Create
            new_page = Page(
                title=title,
                slug=slug,
                lang=lang,
                body=body,
                body_html=body_html,
                is_translated=False,
                is_hidden=is_hidden,
                is_favorite=is_favorite,
                chapter_id=chapter_id,
                favorit_order=1000,
                order=1000,
            )
            db.add(new_page)
            await db.flush()  # get ID
            pid = new_page.id

            # Trigger translation
            from jobs.celery import app as celery_app

            celery_app.send_task("jobs.content.translate_page", args=[pid])

            await db.commit()
            await flash(request, _("Page added"))
            return web.HTTPFound(
                location=request.headers.get(
                    "Referer", f"/admin/support/pages?lang={lang}"
                )
            )

    elif action == "update_page_order":
        ids = json.loads(data.get("ids", "[]"))

        # We need to sync order across languages based on from_id/source.
        # Fetch mapping for provided IDs to find their Source.
        if ids:
            stmt = sa.select(Page.id, Page.from_id).where(Page.id.in_(map(int, ids)))
            res = await db.execute(stmt)
            page_map = dict(res.all())  # id -> from_id

            for idx, pid in enumerate(ids):
                pid = int(pid)
                # Determine Source ID
                # If page has from_id, source is from_id.
                # If from_id is None, it is Source.
                from_id = page_map.get(pid)
                source_id = from_id if from_id else pid

                # Update this page AND all related translations (where from_id == source_id)
                # AND the source itself (where id == source_id)
                await db.execute(
                    sa.update(Page)
                    .values(order=idx)
                    .where(sa.or_(Page.id == source_id, Page.from_id == source_id))
                )

        await db.commit()
        await flash(request, _("Page order updated"))
        return web.json_response({"status": "ok"})

    elif action == "sync_page_translations":
        from jobs.celery import app as celery_app

        pid = int(data.get("id"))
        celery_app.send_task("jobs.content.translate_page", args=[pid])

        await flash(request, _("Translation sync started"))
        if request.headers.get("HX-Request"):
            return web.json_response({"status": "ok"})
        return web.HTTPFound(location=request.headers.get("Referer", "/admin/support"))

    elif action == "delete_page":
        pid = int(data.get("id"))
        await db.execute(sa.delete(Page).where(Page.id == pid))
        await db.commit()

        await flash(request, _("Page deleted"))
        return web.HTTPFound(location=request.headers.get("Referer", "/admin/support"))

    elif action == "toggle_page_hidden":
        pid = int(data.get("id"))
        page = await db.get(Page, pid)
        if page:
            page.is_hidden = not page.is_hidden
            await db.commit()

            from aiohttp_jinja2 import render_string

            current_chapter_id = data.get("current_chapter_id")
            if current_chapter_id:
                current_chapter_id = int(current_chapter_id)

            html = render_string(
                "support/admin/_page_row.html",
                request,
                {
                    "page": page,
                    "current_chapter_id": current_chapter_id,
                },
            )
            await flash(request, _("Visibility toggled"))
            return web.Response(text=html, content_type="text/html")
        return web.Response(status=404)

    elif action == "update_favorites_order":
        ids = json.loads(data.get("ids", "[]"))

        for idx, pid in enumerate(ids):
            await db.execute(
                sa.update(Page).values(favorit_order=idx).where(Page.id == int(pid))
            )
        await db.commit()
        await flash(request, _("Favorites order updated"))
        return web.json_response({"status": "ok"})

    elif action == "reply_ticket":
        import asyncio

        tid = int(data.get("ticket_id"))
        body = data.get("body")
        is_internal = data.get("is_internal") == "on"
        user_id = user.id

        # Determine if Admin
        # This view is for admin so is_by_admin = True.
        # But user_tickets will use same logic? No, create separate or generic.
        # Let's assume support_actions is Admin/Shared?
        # User side might post to separate endpoint or check rights.
        # For now, if user is admin role, is_by_admin=True.
        # But request["user"] is the actor.

        is_admin = user.role.value == "admin"

        if not is_admin and is_internal:
            return web.Response(status=403)

        # Uploads
        uploads_list = []
        if "uploads" in data:
            files = data.getall("uploads")
            from core.utils import save_upload

            for f in files:
                if hasattr(f, "filename") and f.filename:
                    res = await save_upload(f, folder="tickets")
                    if res:
                        uploads_list.append(res)

        comment = TicketComment(
            ticket_id=tid,
            user_id=user_id,
            is_by_admin=is_admin,
            body=body,
            is_internal=is_internal,
            uploads=uploads_list,
        )
        db.add(comment)

        # Update ticket updated_at and status
        await db.execute(
            sa.update(Ticket)
            .values(
                updated_at=sa.func.now(), status="open" if is_admin else "new"
            )  # Reopen if closed? Or simple status update.
            .where(Ticket.id == tid)
        )

        await db.commit()
        await flash(request, _("Reply sent"))

        # Send notification email
        from core.utils import sendmessage

        admin_email = config.get("admin_email")

        # Fetch ticket with user
        ticket = await db.scalar(
            sa.select(Ticket)
            .options(sa.orm.joinedload(Ticket.user))
            .where(Ticket.id == tid)
        )

        if ticket:
            if is_admin:
                # Notify User
                if ticket.user and ticket.user.email:
                    asyncio.create_task(
                        sendmessage(
                            to=ticket.user.email,
                            subject=f"New Reply on Ticket #{ticket.short_id}: {ticket.subject}",
                            template="mail/new_reply.html",
                            request=request,
                            context={"ticket": ticket, "comment": comment},
                        )
                    )
            elif admin_email:
                # Notify Admin
                asyncio.create_task(
                    sendmessage(
                        to=admin_email,
                        subject=f"New Reply on Ticket #{ticket.short_id}",
                        template="mail/new_reply.html",
                        request=request,
                        context={"ticket": ticket, "comment": comment},
                    )
                )

        return web.json_response({"status": "ok"})

    elif action == "close_ticket":
        tid = int(data.get("id"))
        await db.execute(
            sa.update(Ticket)
            .values(status="closed", updated_at=sa.func.now())
            .where(Ticket.id == tid)
        )
        await db.commit()
        return web.Response(text=str(_("Closed")))

    elif action == "reopen_ticket":
        tid = int(data.get("id"))
        await db.execute(
            sa.update(Ticket)
            .values(status="open", updated_at=sa.func.now())
            .where(Ticket.id == tid)
        )
        await db.commit()
        await db.commit()
        return web.Response(text=str(_("Reopened")))

    elif action == "create_ticket":
        subject = data.get("subject")
        body = data.get("body")

        # Uploads
        uploads_list = []
        if "uploads" in data:
            files = data.getall("uploads")
            from core.utils import save_upload

            for f in files:
                if hasattr(f, "filename") and f.filename:
                    res = await save_upload(f, folder="tickets")
                    if res:
                        uploads_list.append(res)

        # Generate short_id
        import uuid

        short_id = str(uuid.uuid4())[:8]

        ticket = Ticket(
            subject=subject,
            user_id=user.id,
            status="new",
            lang=user.language,
            short_id=short_id,
        )
        db.add(ticket)
        await db.flush()

        comment = TicketComment(
            ticket_id=ticket.id,
            user_id=user.id,
            is_by_admin=False,
            body=body,
            is_internal=False,
            uploads=uploads_list,
        )
        db.add(comment)
        await db.commit()

        # Notification
        from core.utils import sendmessage

        admin_email = config.get("admin_email")
        if admin_email:
            asyncio.create_task(
                sendmessage(
                    to=admin_email,
                    subject=f"New Ticket: {subject} (#{short_id})",
                    template="mail/new_ticket.html",
                    request=request,
                    context={"ticket": ticket, "comment": comment},
                )
            )

        return web.HTTPFound(
            request.app.router["user_ticket_detail"].url_for(ticket_id=str(ticket.id))
        )


@login_required()
@aiohttp_jinja2.template("support/user_tickets.html")
async def user_tickets(request):
    db = request["db"]
    user = request["user"]

    stmt = (
        sa.select(Ticket)
        .where(Ticket.user_id == user.id)
        .order_by(Ticket.created_at.desc())
    )
    tickets = (await db.execute(stmt)).scalars().all()

    return {"tickets": tickets}


@login_required()
@aiohttp_jinja2.template("support/user_ticket_detail.html")
async def user_ticket_detail(request):
    db = request["db"]
    user = request["user"]
    ticket_id = int(request.match_info["ticket_id"])

    # Verify ownership
    ticket = await db.scalar(
        sa.select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == user.id)
    )
    if not ticket:
        raise web.HTTPNotFound()

    comments_stmt = (
        sa.select(TicketComment)
        .options(sa.orm.joinedload(TicketComment.user))
        .where(TicketComment.ticket_id == ticket_id, TicketComment.is_internal == False)
        .order_by(TicketComment.created_at.asc())
    )
    comments = (await db.execute(comments_stmt)).scalars().all()

    return {"ticket": ticket, "comments": comments}


async def get_public_sidebar(db, lang):
    chapters = (
        (
            await db.execute(
                sa.select(Chapter)
                .where(Chapter.lang == lang, Chapter.is_hidden == False)
                .order_by(Chapter.order)
            )
        )
        .scalars()
        .all()
    )

    sidebar = []
    for ch in chapters:
        pages = (
            (
                await db.execute(
                    sa.select(Page)
                    .where(Page.chapter_id == ch.id, Page.is_hidden == False)
                    .order_by(Page.order)
                )
            )
            .scalars()
            .all()
        )
        sidebar.append({"chapter": ch, "pages": pages})
    return sidebar


async def get_suggested_url(request, db, current_lang, obj=None):
    """
    Returns (suggested_url, suggested_lang_name, hreflangs)
    """
    user = request.get("user")
    supported = config.get("lang_supported", {"en": "English"})

    # Determine target lang
    target_lang = "en"
    if user:
        target_lang = user.language
    else:
        # Try to guess from header or cookie, or valid default
        # For now default to 'en' or first supported
        pass

    if target_lang not in supported:
        target_lang = "en"

    # Hreflangs: list of {lang: code, url: url}
    hreflangs = []

    # If already on target lang, no suggestion needed (unless we want to show banner anyway? No.)
    suggested_url = None
    suggested_lang_name = None

    if obj:
        # Article logic
        # Find siblings: where (from_id = X or id = X)
        # Identify group ID
        group_id = obj.from_id if obj.from_id else obj.id

        # Fetch all siblings
        siblings_stmt = sa.select(Page).where(
            sa.or_(Page.id == group_id, Page.from_id == group_id),
            Page.is_hidden == False,
        )
        siblings = (await db.execute(siblings_stmt)).scalars().all()

        # Build map lang -> slug
        lang_map = {p.lang: p.slug for p in siblings}

        # Build hreflangs
        for l_code, l_name in supported.items():
            if l_code in lang_map:
                url = request.app.router["public_article"].url_for(
                    lang=l_code, slug=lang_map[l_code]
                )
                hreflangs.append({"lang": l_code, "url": url})

        # Suggestion
        if current_lang != target_lang and target_lang in lang_map:
            suggested_url = request.app.router["public_article"].url_for(
                lang=target_lang, slug=lang_map[target_lang]
            )
            suggested_lang_name = supported[target_lang]

    else:
        # Home logic
        # Hreflangs are just root /lang/support
        for l_code, l_name in supported.items():
            url = request.app.router["public_support"].url_for(lang=l_code)
            hreflangs.append({"lang": l_code, "url": url})

        if current_lang != target_lang:
            suggested_url = request.app.router["public_support"].url_for(
                lang=target_lang
            )
            suggested_lang_name = supported[target_lang]

    return suggested_url, suggested_lang_name, hreflangs


async def support_root(request):
    user = request.get("user")
    supported = config.get("lang_supported", {"en": "English"})
    target_lang = "en"

    if user and user.language in supported:
        target_lang = user.language

    url = request.app.router["public_support"].url_for(lang=target_lang)
    return web.HTTPFound(url)


@aiohttp_jinja2.template("support/public/home.html")
async def public_support(request):
    db = request["db"]
    lang = request.match_info.get("lang", "en")

    # Favorites
    favorites = (
        (
            await db.execute(
                sa.select(Page)
                .where(
                    Page.is_favorite == True, Page.lang == lang, Page.is_hidden == False
                )
                .order_by(Page.favorit_order)
            )
        )
        .scalars()
        .all()
    )

    sidebar = await get_public_sidebar(db, lang)

    suggested_url, suggested_lang_name, hreflangs = await get_suggested_url(
        request, db, lang, obj=None
    )

    return {
        "favorites": favorites,
        "sidebar": sidebar,
        "lang": lang,
        "request": request,  # for url helper if needed
        "suggested_url": suggested_url,
        "suggested_lang_name": suggested_lang_name,
        "hreflangs": hreflangs,
    }


@aiohttp_jinja2.template("support/public/article.html")
async def public_article(request):
    db = request["db"]
    lang = request.match_info.get("lang", "en")
    slug = request.match_info["slug"]

    page = await db.scalar(sa.select(Page).where(Page.slug == slug, Page.lang == lang))
    if not page or page.is_hidden:
        raise web.HTTPNotFound()

    sidebar = await get_public_sidebar(db, lang)

    suggested_url, suggested_lang_name, hreflangs = await get_suggested_url(
        request, db, lang, obj=page
    )

    return {
        "page": page,
        "sidebar": sidebar,
        "lang": lang,
        "current_page": page,
        "request": request,
        "suggested_url": suggested_url,
        "suggested_lang_name": suggested_lang_name,
        "hreflangs": hreflangs,
    }
