import logging
import sys
import traceback
from collections import namedtuple

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp.helpers import DEBUG
from aiohttp.web_middlewares import normalize_path_middleware
from aiohttp_session import get_session, session_middleware
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from sentry_sdk import capture_exception

from vchat.app_keys import REDIS_KEY
from vchat.db import async_session_factory
from vchat.text import _
from vchat.models import User
from vchat.utils import Meta

from .cors import cors_middleware
from .https import https_middleware

logger = logging.getLogger(__name__)


@web.middleware
async def meta_middleware(request: web.Request, handler) -> web.StreamResponse:
    request["meta"] = Meta()
    return await handler(request)


async def handle_error(request: web.Request, code: int = 404) -> web.Response:
    if not request.get("meta"):
        request["meta"] = Meta()
    request["meta"].title = _("Error {code}").format(code=code)
    return aiohttp_jinja2.render_template(f"misc/{code}.html", request, {}, status=code)


@web.middleware
async def error_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    # Disable error handling for development
    if DEBUG:
        if not request.headers.get("Authorization"):
            return await handler(request)

        try:
            return await handler(request)
        except web.HTTPException as ex:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            t = traceback.format_exception(exc_type, exc_value, exc_traceback)
            logger.info("Exception: ", exc_info=ex)
            return web.Response(text="".join(t), status=500)
    # Prod handling errors
    errors = (403, 404, 405, 500)

    try:
        return await handler(request)
    except web.HTTPException as ex:
        if ex.status == 404:
            logger.warning("Not Found (%s %s)", request.method, request.rel_url)
        elif ex.status == 500:
            logger.error("Server Error", exc_info=True)
            capture_exception(ex)

        if ex.status in errors:
            return await handle_error(request, code=ex.status)

        # we don't handle this error
        raise
    except Exception as ex:
        logger.info("Server Error", exc_info=True)
        capture_exception(ex)

        return await handle_error(request, code=500)


@web.middleware
async def debug_access_control_header_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    handler = await handler(request)
    handler.headers["Access-Control-Allow-Origin"] = "*"
    handler.headers["Access-Control-Allow-Credentials"] = "true"
    handler.headers["Access-Control-Allow-Headers"] = (
        "accept, accept-encoding, authorization, "
        "content-type, dnt, origin, user-agent, "
        "x-csrftoken, x-requested-with, "
        "upload-length, upload-metadata, "
        "upload-offset, location"
    )
    handler.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD"
    )
    handler.headers["Access-Control-Expose-Headers"] = (
        "upload-length, upload-metadata, upload-offset, location"
    )
    return handler


@web.middleware
async def db_session_middleware(request, handler):
    # Skip static files
    if request.path.startswith("/static/"):
        return await handler(request)

    # Create a database session
    async with async_session_factory() as session:
        request["db"] = session
        try:
            response = await handler(request)
            return response
        except Exception as e:
            await session.rollback()
            raise e


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: web.RequestHandler,
) -> web.RequestHandler:
    # Skip static files
    if request.path.startswith("/static/"):
        return await handler(request)

    try:
        request["auth_session"] = await get_session(request)
        request["user"] = None

        user_id = request["auth_session"].get("staff_id")
        if user_id is not None:
            result = await request["db"].execute(
                sa.select(User).where(User.id == user_id)
            )
            user = result.scalars().first()
            if user:
                request["user"] = user
            else:
                request["auth_session"].invalidate()
    except Exception as e:
        logger.error("Error in auth_middleware: %s", e)
        return web.Response(text="Internal Server Error", status=500)

    return await handler(request)


Msg_type = namedtuple("Msg", ["status", "message"])


@web.middleware
async def flash_middleware(request, handler):
    """
    - Load the user's pending flash messages from Redis.
    - Put them into request['flash_messages'] so they're accessible to the template.
    - Clear them from Redis once loaded (so they don't persist).
    """
    user = request.get("user")

    if user:
        r = request.app[REDIS_KEY]
        key = f"message_{user.id}"

        # Retrieve all messages, then clear them
        msgs = await r.lrange(key, 0, -1)  # Returns list of JSON strings
        await r.delete(key)  # Clear after retrieval

        # Parse them; store on request for a context processor
        request["flash_messages"] = [Msg_type(*i.decode().split("|")) for i in msgs]

    return await handler(request)


@web.middleware
async def force_https_location_middleware(request, handler):
    response = await handler(request)
    location = response.headers.get("Location")
    if location and location.startswith("http://"):
        from vchat.app_keys import CONFIG_KEY

        config = request.app[CONFIG_KEY]
        public_url = config.get("public_url", "")
        if public_url.startswith("https://"):
            response.headers["Location"] = location.replace("http://", "https://", 1)
    return response


def get_middlewares(config):
    ORIGINS = [
        "https://www.vchat.com",
        "https://vchat.com",
        "https://local.vchat.com",
    ]
    CORS_METHODS = ("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD")
    CORS_EXPOSE_HEADERS = (
        "tus-resumable",
        "upload-length",
        "upload-metadata",
        "upload-offset",
        "location",
    )

    access_control_middleware = (
        cors_middleware(
            allow_all=True,
            origins=ORIGINS,
            # allow_credentials=True,
            allow_methods=CORS_METHODS,
            expose_headers=CORS_EXPOSE_HEADERS,
            # urls=[re.compile(r"^\/api")],
        )
        if not DEBUG
        else debug_access_control_header_middleware
    )

    middlewares = (
        # Origin Policy
        access_control_middleware,
        # First, normalize request path, which may result in redirect
        normalize_path_middleware(append_slash=True, merge_slashes=True),
        # After, enable error middleware to catch all errors from any code below
        error_middleware,
        # Now enable session middleware and API auth / user / admin stuff
        session_middleware(
            EncryptedCookieStorage(
                config["cookie_key"],
                cookie_name=config["cookie_name"],
                domain=config["cookie_domain"],
                secure=config["cookie_secure"],
                max_age=30 * 24 * 60 * 60,
                path="/",
            )
        ),
        meta_middleware,
        db_session_middleware,
        auth_middleware,
        flash_middleware,
        force_https_location_middleware,
    )

    if config["enable_https_middleware"]:
        middlewares = (https_middleware(), *middlewares)

    return middlewares


__all__ = ["get_middlewares"]
