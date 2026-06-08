import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlsplit

from jobs.crawler.tasks import crawl_page_task
from vchat.app_keys import CONFIG_KEY, SIGNER_KEY
from vchat.json_response import json_response
from vchat.models import Page, Source, WidgetIntegration
from vchat.page_status import PageStatus
from vchat.triggers import (
    canonical_page_url,
    find_page_by_url,
    load_default_trigger_templates,
    page_trigger_items,
    render_default_triggers,
    source_trigger_rules_match_url,
)


def _url_host(value: str) -> str:
    return (urlsplit((value or "").strip()).netloc or "").lower()


async def _source_for_widget_url(db, page_url: str) -> Source | None:
    host = _url_host(page_url)
    if not host:
        return None
    sources = list((await db.execute(sa.select(Source))).scalars())
    for source in sources:
        if _url_host(source.uri) == host:
            return source
    for source in sources:
        source_host = _url_host(source.uri)
        if source_host and host.endswith(f".{source_host}"):
            return source
    return None


async def _discover_widget_page(
    request,
    *,
    source: Source,
    page_url: str,
) -> Page | None:
    if getattr(source, "is_paused", False) or getattr(source, "blocked_reason", None):
        return None

    uri = canonical_page_url(page_url)
    if not uri:
        return None

    db = request["db"]
    page = await find_page_by_url(db, uri)
    if page is not None:
        return page

    page = Page(
        source_id=source.id,
        uri=uri,
        status=PageStatus.crawler,
        has_triggers=True,
        discover_by="widget",
        discover_source=uri,
    )
    page._hash = ""
    db.add(page)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        page = await find_page_by_url(db, uri)
        if page is not None:
            return page
        raise
    page_id = page.id
    await db.commit()
    crawl_page_task.delay(page_id)
    return page


def _signed_trigger_page_token(request, page: Page) -> str:
    return request.app[SIGNER_KEY].dumps(page.id, salt="trigger_page")


async def _json_response_after_rollback(request, payload):
    await request["db"].rollback()
    return json_response(payload)


async def healthcheck(request):
    await request["db"].execute(sa.text("select 1;"))
    raise web.HTTPFound(request.app.router["project_view"].url_for())


robots = """
User-agent: *
Disallow: /

Host: chat.vbudushee.ru
"""


async def robots_txt(request):
    return web.Response(text=robots, content_type="text/plain")


async def favicon(request):
    raise web.HTTPFound("/static/favicon.ico")


async def widget_js(request):
    code = request.match_info.get("code", "").strip()
    exists = await request["db"].scalar(
        sa.select(WidgetIntegration.id).where(WidgetIntegration.code == code)
    )
    if not exists:
        await request["db"].rollback()
        raise web.HTTPNotFound(text="Widget code not found")
    await request["db"].rollback()
    widget_chat_path = str(request.app.router["public_widget_chat"].url_for(code=code))
    trigger_resolve_path = str(request.app.router["widget_triggers_resolve"].url_for())
    return aiohttp_jinja2.render_template(
        "js/widget.js",
        request,
        {
            "widget_code": code,
            "widget_chat_path": widget_chat_path,
            "trigger_resolve_path": trigger_resolve_path,
        },
    )


async def widget_triggers_resolve(request):
    page_url = request.query.get("url", "")
    title = request.query.get("title", "")
    page = await find_page_by_url(request["db"], page_url)
    source = await _source_for_widget_url(request["db"], page_url)
    if source is not None and not source.enable_triggers:
        return await _json_response_after_rollback(
            request,
            {
                "triggers": [],
            },
        )
    if page is not None:
        if source is None and page.source_id:
            source = await request["db"].scalar(
                sa.select(Source).where(Source.id == page.source_id)
            )
        if not source or not source.enable_triggers:
            return await _json_response_after_rollback(
                request,
                {
                    "triggers": [],
                },
            )
        if not source_trigger_rules_match_url(source, page_url):
            return await _json_response_after_rollback(
                request,
                {
                    "triggers": [],
                },
            )
        triggers = page_trigger_items(page)
        if triggers:
            return await _json_response_after_rollback(
                request,
                {
                    "page_token": _signed_trigger_page_token(request, page),
                    "triggers": [
                        {
                            "key": trigger["key"],
                            "text": trigger["text"],
                        }
                        for trigger in triggers
                    ],
                },
            )

    if source is not None and not source_trigger_rules_match_url(source, page_url):
        return await _json_response_after_rollback(
            request,
            {
                "triggers": [],
            },
        )
    if (
        page is None
        and source is not None
        and request.app[CONFIG_KEY]["widget_page_discovery_enabled"]
    ):
        page = await _discover_widget_page(request, source=source, page_url=page_url)

    default_title = title or (page.title if page is not None else "")
    return await _json_response_after_rollback(
        request,
        {
            "triggers": render_default_triggers(
                load_default_trigger_templates(request.app),
                default_title,
            ),
        },
    )


@aiohttp_jinja2.template("demo.html")
async def demo_page(request):
    widget_rows = (
        await request["db"].execute(
            sa.select(
                WidgetIntegration.id,
                WidgetIntegration.name,
                WidgetIntegration.code,
            ).order_by(WidgetIntegration.name.asc(), WidgetIntegration.id.asc())
        )
    ).all()
    trigger_candidates = (
        (
            await request["db"].execute(
                sa.select(Page)
                .join(Source, Source.id == Page.source_id)
                .where(Page.has_triggers.is_(True), Page.triggers.isnot(None))
                .where(Source.enable_triggers.is_(True))
                .order_by(Page.updated_at.desc().nullslast(), Page.id.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    selected_code = request.query.get("code", "").strip()
    selected_trigger_url = request.query.get("trigger_url", "").strip()
    widgets = [
        {
            "id": widget_id,
            "name": name,
            "code": code,
        }
        for widget_id, name, code in widget_rows
    ]
    trigger_pages = [
        {
            "id": page.id,
            "title": page.title or page.uri,
            "uri": page.uri,
        }
        for page in trigger_candidates
        if page_trigger_items(page)
    ][:3]
    await request["db"].rollback()
    if selected_code and selected_code not in {widget["code"] for widget in widgets}:
        selected_code = ""
    trigger_page_urls = {page["uri"] for page in trigger_pages}
    return {
        "widgets": widgets,
        "trigger_pages": trigger_pages,
        "selected_widget_code": selected_code,
        "selected_trigger_url": selected_trigger_url,
        "selected_trigger_url_is_listed": selected_trigger_url in trigger_page_urls,
    }
