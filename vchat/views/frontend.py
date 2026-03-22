import re
from pathlib import Path

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web

from vchat.app_keys import CONFIG_KEY
from vchat.i18n import lazy_gettext as _
from vchat.models import Project, User
from vchat.utils import convert_to_html, login_required, meta


@meta(title=_("Welcome to vchat"))
@aiohttp_jinja2.template("frontend/index.html")
async def index(_):
    return {}


@meta(title=_("Prices"))
@aiohttp_jinja2.template("frontend/prices.html")
async def prices(_):
    return {}


re_pattern = re.compile("[a-z0-9-_.]+")


async def healthcheck(request):
    await request["db"].execute("select 1;")
    return web.HTTPFound(request.app.router["index"].url_for())


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
    # Serve the widget.js file, potentially injecting configuration if needed
    # For now, just serve the static file or render a template if we need dynamic values
    # The user request says "copy and paste special javascript code... <script src='.../js?project={project.id}'></script>"
    # So we might need to use the project_id from query param if we want to bake it in,
    # but the example usage shows project_id in the src URL, which is fine.
    # The JS itself will likely read the script tag src or attributes to get the project ID,
    # or we can render it into the JS.
    # Let's render it as a template to be flexible.
    return aiohttp_jinja2.render_template("js/widget.js", request, {})
