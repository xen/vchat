import logging
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from collections import namedtuple
from typing import Any

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp.helpers import DEBUG
from aiohttp.web_middlewares import normalize_path_middleware
from aiohttp_session import get_session, session_middleware
from aiohttp_session.cookie_storage import EncryptedCookieStorage

from vchat.settings import CONFIG_KEY, REDIS_KEY
from vchat.db import async_session_factory
from vchat.models import User, UserSession
from vchat.tracing import (
    REQUEST_ID_HEADER,
    generate_request_id,
    normalize_request_id,
    request_id_ctx,
)
from vchat.utils import Meta

from .cors import cors_middleware
from .https import https_middleware

logger = logging.getLogger(__name__)

RequestHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
PUBLIC_WIDGET_CORS_PATHS = {"/api/triggers/resolve"}


def _is_public_widget_frame(request: web.Request) -> bool:
    return request.path.startswith("/chat/widget/")


@web.middleware
async def request_id_middleware(
    request: web.Request,
    handler: RequestHandler,
) -> web.StreamResponse:
    request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
    if request_id is None:
        request_id = generate_request_id()

    request["request_id"] = request_id
    token = request_id_ctx.set(request_id)
    try:
        response = await handler(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        request_id_ctx.reset(token)


@web.middleware
async def reject_trace_middleware(
    request: web.Request,
    handler: RequestHandler,
) -> web.StreamResponse:
    if request.method == "TRACE":
        raise web.HTTPMethodNotAllowed(request.method, ("GET", "HEAD", "OPTIONS"))
    return await handler(request)


@web.middleware
async def meta_middleware(request: web.Request, handler) -> web.StreamResponse:
    request["meta"] = Meta()
    return await handler(request)


async def handle_error(request: web.Request, code: int = 404) -> web.Response:
    if not request.get("meta"):
        request["meta"] = Meta()
    request["meta"].title = "Ошибка {code}".format(code=code)
    return aiohttp_jinja2.render_template(f"misc/{code}.html", request, {}, status=code)


@web.middleware
async def error_middleware(
    request: web.Request, handler: RequestHandler
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
        if ex.status == 403:
            logger.warning(
                "Forbidden (%s %s)",
                request.method,
                request.rel_url,
            )
        elif ex.status == 404:
            logger.warning("Not Found (%s %s)", request.method, request.rel_url)
        elif ex.status == 500:
            logger.error("Server Error", exc_info=True)

        if ex.status in errors:
            return await handle_error(request, code=ex.status)

        # we don't handle this error
        raise
    except Exception:
        logger.info("Server Error", exc_info=True)

        return await handle_error(request, code=500)


@web.middleware
async def debug_access_control_header_middleware(
    request: web.Request, handler: RequestHandler
) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = (
        "accept, accept-encoding, authorization, "
        "content-type, dnt, origin, user-agent, "
        "x-csrftoken, x-requested-with, "
        "upload-length, upload-metadata, "
        "upload-offset, location, x-request-id"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD"
    )
    response.headers["Access-Control-Expose-Headers"] = (
        "upload-length, upload-metadata, upload-offset, location, x-request-id"
    )
    return response


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: RequestHandler,
) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    if not request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    csp_parts = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self'",
        "img-src 'self' data: https:",
        "font-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "connect-src 'self' https: wss:",
        "frame-src 'self'",
    ]
    if not _is_public_widget_frame(request):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        csp_parts.insert(3, "frame-ancestors 'self'")
    response.headers.setdefault("Content-Security-Policy", "; ".join(csp_parts))
    if request.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@web.middleware
async def public_widget_cors_middleware(
    request: web.Request,
    handler: RequestHandler,
) -> web.StreamResponse:
    if request.path not in PUBLIC_WIDGET_CORS_PATHS:
        return await handler(request)

    origin = request.headers.get("Origin")
    if request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers:
        response: web.StreamResponse = web.Response(text="")
    else:
        response = await handler(request)

    if origin:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "accept, content-type, origin"
        response.headers["Vary"] = "Origin"
    return response


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
            if session.in_transaction():
                logger.error(
                    "DB transaction left open while rendering %s %s; rolling back",
                    request.method,
                    getattr(request, "path_qs", request.path),
                )
                await session.rollback()
            return response
        except Exception:
            if session.in_transaction():
                await session.rollback()
            raise


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: RequestHandler,
) -> web.StreamResponse:
    # Skip static files
    if request.path.startswith("/static/"):
        return await handler(request)

    request["auth_session"] = await get_session(request)
    request["user"] = None

    user_id = request["auth_session"].get("user_id")
    if user_id is not None:
        session_id = request["auth_session"].get("session_id")
        if not session_id:
            request["auth_session"].invalidate()
            return await handler(request)

        config = request.app.get(CONFIG_KEY, {})
        auth_session_time = int(config.get("auth_session_time", 0) or 0)
        if _auth_session_expired(request["auth_session"], auth_session_time):
            request["auth_session"].invalidate()
            return await handler(request)

        result = await request["db"].execute(
            sa.select(
                User.id,
                User.email,
                User.name,
                User.is_active,
                UserSession.id.label("auth_user_session_id"),
            )
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                User.id == user_id,
                User.is_active.is_(True),
                UserSession.session_id == session_id,
                UserSession.revoked_at.is_(None),
            )
        )
        row = result.first()
        if row:
            request["user"] = UserInfo(
                id=row.id,
                email=row.email,
                name=row.name,
                is_active=row.is_active,
            )
            request["auth_user_session_id"] = row.auth_user_session_id
        else:
            request["auth_session"].invalidate()
        if request["db"].in_transaction():
            await request["db"].rollback()

    return await handler(request)


UserInfo = namedtuple(
    "UserInfo",
    ["id", "email", "name", "is_active"],
    defaults=("", "", True),
)
Msg = namedtuple("Msg", ["status", "message"])


def _auth_session_expired(auth_session: dict[str, Any], ttl_seconds: int) -> bool:
    if ttl_seconds <= 0:
        return False
    login_at = auth_session.get("login_at")
    if not isinstance(login_at, int):
        return True
    return int(time.time()) - login_at >= ttl_seconds


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
        msgs: list[Any] = await r.lrange(key, 0, -1)
        await r.delete(key)

        # Parse them; store on request for a context processor
        request["flash_messages"] = [Msg(*i.decode().split("|")) for i in msgs]

    return await handler(request)


@web.middleware
async def force_https_location_middleware(request, handler):
    response = await handler(request)
    location = response.headers.get("Location")
    if location and location.startswith("http://"):
        config = request.app[CONFIG_KEY]
        public_url = config.get("public_url", "")
        if public_url.startswith("https://"):
            response.headers["Location"] = location.replace("http://", "https://", 1)
    return response


def get_middlewares(config):
    configured_origins = config.get("allowed_origins") or [config.get("public_url", "")]
    origins = tuple(origin for origin in configured_origins if origin)
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
            allow_all=False,
            origins=origins,
            # allow_credentials=True,
            allow_methods=CORS_METHODS,
            expose_headers=(*CORS_EXPOSE_HEADERS, "x-request-id"),
            # urls=[re.compile(r"^\/api")],
        )
        if not DEBUG
        else debug_access_control_header_middleware
    )

    middlewares = (
        # Origin Policy
        public_widget_cors_middleware,
        access_control_middleware,
        reject_trace_middleware,
        request_id_middleware,
        security_headers_middleware,
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
                max_age=int(config["session_max_age_seconds"]),
                path="/",
                httponly=True,
                samesite="Lax",
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
