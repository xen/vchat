import aiohttp_jinja2
from aiohttp import web


async def healthcheck(request):
    await request["db"].execute("select 1;")
    return web.HTTPFound(request.app.router["project_view"].url_for())


robots = """
User-agent: *
Disallow: /

Host: chat.vbudushee.ru
"""


async def robots_txt(request):
    return web.Response(text=robots, content_type="text/plain")


async def favicon(request):
    return web.HTTPFound("/static/favicon.ico")


async def widget_js(request):
    widget_chat_path = str(request.app.router["public_widget_chat"].url_for())
    return aiohttp_jinja2.render_template(
        "js/widget.js", request, {"widget_chat_path": widget_chat_path}
    )
