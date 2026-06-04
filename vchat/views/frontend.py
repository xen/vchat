import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from urllib.parse import urlsplit

from vchat.models import Source
from vchat.triggers import (
    find_page_by_url,
    load_default_trigger_templates,
    page_trigger_items,
    render_default_triggers,
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
    widget_chat_path = str(request.app.router["public_widget_chat"].url_for())
    trigger_resolve_path = str(request.app.router["widget_triggers_resolve"].url_for())
    return aiohttp_jinja2.render_template(
        "js/widget.js",
        request,
        {
            "widget_chat_path": widget_chat_path,
            "trigger_resolve_path": trigger_resolve_path,
        },
    )


async def widget_triggers_resolve(request):
    page_url = request.query.get("url", "")
    title = request.query.get("title", "")
    page = await find_page_by_url(request["db"], page_url)
    source = await _source_for_widget_url(request["db"], page_url)
    if source is not None and not source.config.allow_custom_triggers:
        return web.json_response(
            {
                "page_id": page.id if page is not None else None,
                "source": "disabled",
                "triggers": [],
            }
        )
    if page is not None:
        if source is None and page.source_id:
            source = await request["db"].scalar(
                sa.select(Source).where(Source.id == page.source_id)
            )
        if not source or not source.config.allow_custom_triggers:
            return web.json_response(
                {
                    "page_id": page.id,
                    "source": "disabled",
                    "triggers": [],
                }
            )
        triggers = page_trigger_items(page)
        if triggers:
            return web.json_response(
                {
                    "page_id": page.id,
                    "source": "page",
                    "triggers": [
                        {
                            "page_id": page.id,
                            "key": trigger["key"],
                            "text": trigger["text"],
                            "source": trigger["source"],
                        }
                        for trigger in triggers
                    ],
                }
            )

    default_title = title or (page.title if page is not None else "")
    return web.json_response(
        {
            "page_id": page.id if page is not None else None,
            "source": "default",
            "triggers": render_default_triggers(
                load_default_trigger_templates(request.app),
                default_title,
            ),
        }
    )


@aiohttp_jinja2.template("demo.html")
async def demo_page(request):
    return {}
