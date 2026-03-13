import re
from pathlib import Path

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web

from core.app_keys import CONFIG_KEY
from core.i18n import lazy_gettext as _
from core.models import Project, ProjectUser, User
from core.utils import convert_to_html, login_required, meta


@meta(title=_("Welcome to core"))
@aiohttp_jinja2.template("frontend/index.html")
async def index(_):
    return {}


@meta(title=_("Prices"))
@aiohttp_jinja2.template("frontend/prices.html")
async def prices(_):
    return {}


@login_required()
async def projects(request):
    # Find first available project
    project = await request["db"].scalar(
        sa.select(Project)
        .join(ProjectUser, ProjectUser.project_id == Project.id)
        .where(ProjectUser.user_id == request["user"].id)
        .limit(1)
    )

    if project:
        raise web.HTTPFound(
            request.app.router["project_view"].url_for(project_id=project.short_id)
        )

    # If no project, redirect to onboarding
    raise web.HTTPFound(request.app.router["project_onboarding"].url_for())


re_pattern = re.compile("[a-z0-9-_.]+")


@aiohttp_jinja2.template("frontend/page.html")
async def page(request):
    page_id = request.match_info.get("page", "index")
    # Language is determined by middleware and stored in request["lang"]
    lang = request.get("lang", "en")

    if not re_pattern.fullmatch(page_id):
        return web.HTTPNotFound()

    # Try to find the file in the requested language
    path = Path(
        Path(__file__) / ".." / ".." / "docs" / lang / f"{page_id}.md"
    ).resolve()

    # Fallback to English if the specific language file doesn't exist
    if not path.exists() and lang != "en":
        path = Path(
            Path(__file__) / ".." / ".." / "docs" / "en" / f"{page_id}.md"
        ).resolve()

    if not path.exists():
        raise web.HTTPNotFound()

    text = path.read_text(encoding="utf-8")
    content, _meta = convert_to_html(text)
    request["meta"].update(**_meta)

    return {"content": content}


async def healthcheck(request):
    await request["db"].execute("select 1;")
    return web.HTTPFound(request.app.router["index"].url_for())


async def set_language(request):
    lang = request.match_info.get("lang")
    if lang not in request.app[CONFIG_KEY]["lang_supported"]:
        raise web.HTTPFound("/")

    target = (
        request.rel_url
        if not request.rel_url.raw_path.startswith("/set_lang/")
        else "/"
    )
    response = web.HTTPFound(target)

    if request.get("user"):
        await request["db"].execute(
            sa.update(User).where(User.id == request["user"].id).values(language=lang)
        )
        await request["db"].commit()
    else:
        response.set_cookie(
            "language", lang, max_age=3600 * 24 * 30
        )  # Cookie valid for 30 days

    return response


robots = """
User-agent: *
Allow: /

Host: www.core.com
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
