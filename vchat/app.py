import logging
from datetime import datetime
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web
from aiohttp.helpers import DEBUG
from aiohttp.web_app import Application
from itsdangerous import URLSafeTimedSerializer
from jinja2 import Environment
from redis.asyncio import from_url as redis_from_url

from vchat.app_keys import (
    CONFIG_KEY,
    LOGGER_KEY,
    REDIS_KEY,
    SETTINGS_KEY,
    SIGNER_KEY,
)
from vchat.db import engine
from vchat.i18n import _
from vchat.metrics import validate_multiprocess_setup
from vchat.project_settings import init_settings_cache
from vchat.utils import (
    make_full_url,
    protect,
    protect_timed,
)
from .middlewares import get_middlewares

__all__ = ("create_app",)

logger = logging.getLogger(__name__)


async def create_app() -> Application:
    from .routes import setup_routes
    from .settings import config

    validate_multiprocess_setup()

    redis = redis_from_url(config["redis_uri"])
    # 5 MB should be enough for everyone // Bill Gates
    app = web.Application(
        client_max_size=config["max_upload_size"], middlewares=get_middlewares(config)
    )
    domain = None
    if not DEBUG:
        domain = config.get("cookie_domain")
        if not domain:
            logger.warning("cookie_domain must be set in production")

    app[CONFIG_KEY] = config
    app[LOGGER_KEY] = logger
    app[REDIS_KEY] = redis
    app[SIGNER_KEY] = URLSafeTimedSerializer(config["secret_key"])
    app[SETTINGS_KEY] = {}
    await init_settings_cache(app)

    # Setup base Jinja2
    aiohttp_jinja2.setup(
        app,
        context_processors=[
            init_jinja,
            aiohttp_jinja2.request_processor,
        ],
        loader=jinja2.PackageLoader("vchat", "templates"),
    )
    app[aiohttp_jinja2.static_root_key] = "/static/"
    try:
        assets_dir = Path("frontend/dist/assets")
        if assets_dir.exists():
            latest_mtime = max(
                (p.stat().st_mtime for p in assets_dir.glob("*") if p.is_file()),
                default=datetime.now().timestamp(),
            )
            app["static_version"] = str(int(latest_mtime))
        else:
            app["static_version"] = str(int(datetime.now().timestamp()))
    except Exception:
        app["static_version"] = str(int(datetime.now().timestamp()))
    # Add the custom filter
    jinja_env: Environment = aiohttp_jinja2.get_env(app)
    jinja_env.filters["protect"] = protect
    jinja_env.filters["protect_timed"] = protect_timed

    # Setup routes
    setup_routes(app)

    async def close_redis(app):
        await app[REDIS_KEY].aclose()

    async def dispose_db(app):
        await engine.dispose()

    app.on_cleanup.append(close_redis)
    app.on_cleanup.append(dispose_db)

    return app


async def init_jinja(request):
    def csrf_token() -> str:
        if request.get("user"):
            return request.app[SIGNER_KEY].dumps(request.get("user").id)
        return ""

    return {
        "config": request.app[CONFIG_KEY],
        "len": len,
        "debug": DEBUG,
        "meta": request.get("meta"),
        "user": request.get("user"),
        "current_year": datetime.now().year,
        "pretty_date": lambda date: date.strftime("%Y-%m-%d") if date else "",
        "pretty_datetime": lambda date: (
            date.strftime("%Y-%m-%d %H:%M:%S") if date else ""
        ),
        "get_flash_messages": lambda: request.get("flash_messages", []),
        "external": lambda x, **kwargs: make_full_url(request, x, **kwargs),
        "url_for": lambda x, **kwargs: request.app.router[x].url_for(**kwargs),
        "csrf_token": csrf_token,
        "project_settings": request.app[SETTINGS_KEY],
        "static_version": request.app.get("static_version", ""),
        "_": _,
    }
