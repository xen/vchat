import asyncio
import base64
import logging
import os
import uuid
from math import ceil
from datetime import datetime
from functools import wraps
from typing import Callable, Optional, Tuple
from urllib.parse import urlencode

import markdown
import msgspec
import redis.asyncio as aioredis
import sqlalchemy as sa
from aiohttp import web
from aiohttp.abc import AbstractCookieJar, AbstractView
from aiohttp_session import get_session
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)
from multidict import CIMultiDict, CIMultiDictProxy, istr
from yarl import URL

from vchat.settings import CONFIG_KEY, REDIS_KEY, SIGNER_KEY
from vchat.settings import config
from vchat.tracing import REQUEST_ID_HEADER, get_request_id


class _MsgSpecJSON:
    # from performance tips this recommendation
    _encoder = msgspec.json.Encoder()
    _decoder = msgspec.json.Decoder()

    def dumps(self, obj, **kwargs):
        # Encode to bytes, then decode to UTF-8 string
        # `msgspec` does not support stdlib flags like `ensure_ascii`,
        # but some call sites still pass them through.
        return self._encoder.encode(obj).decode("utf-8")

    def dump(self, obj, fp, **kwargs):
        # Write the UTF-8 JSON string into a file-like object
        fp.write(self.dumps(obj, **kwargs))

    def loads(self, s):
        # If s is a string, convert to bytes; if it’s already bytes, just use it
        if isinstance(s, str):
            s = s.encode("utf-8")
        # Decode into a Python object
        return self._decoder.decode(s)

    def load(self, fp):
        # Read from a file-like object as a string and decode
        return self.loads(fp.read())


# Create a single namespace object
json = _MsgSpecJSON()


def encode_json(obj) -> bytes:
    return msgspec.json.encode(obj)


def json_response(
    data,
    *,
    status: int = 200,
    reason: str | None = None,
    headers: (
        dict[str | istr, str] | CIMultiDict[str] | CIMultiDictProxy[str] | None
    ) = None,
    content_type: str = "application/json",
) -> web.Response:
    return web.Response(
        body=encode_json(data),
        status=status,
        reason=reason,
        headers=headers,
        content_type=content_type,
    )


REDIS_URL = config.get("redis_uri")
CELERY_DEFAULT_QUEUE = config.get("celery_default_queue", "celery")

DELAY_PROTECTION = 5

logger = logging.getLogger()


def to_str(item: Optional[str]) -> str:
    if isinstance(item, str):
        return item
    elif isinstance(item, list):
        return "".join(item)
    return str(item)


class Meta:
    title = "Заголовок"
    author = "vchat"
    description = ""
    props = {}

    def update(self, **kwargs) -> None:
        """Using description protocol"""
        for k, v in kwargs.items():
            self.__dict__[k] = to_str(v)

    def __get__(self, obj, owner):
        return self.props.get(obj, "")

    def __set__(self, obj, value):
        self.props[obj] = value

    def __repr__(self):
        return f"""<Meta title='{self.title}', author='{self.author}',
    description='{self.description}'>"""


def meta(*, title="", author="", description="", **kwargs) -> Callable:
    def wrapper(func):
        @wraps(func)
        async def wrapped(*args):
            # Supports class based views see web.View
            request = args[0].request if isinstance(args[0], AbstractView) else args[-1]
            if "meta" not in request:
                request["meta"] = Meta()
            request["meta"].update(
                title=title or "",
                author=author or "",
                description=description or "",
                **kwargs,
            )

            return await func(*args)

        return wrapped

    return wrapper


async def flash(request, message, category="success"):
    """
    Send notification to redis, so it can be displayed on the page
    Sister function is added to jinja conetext processor `get_flashed_messages`.
    Don't add `|` to the message, it will be removed for safety.
    """
    user = request.get("user")
    if not user:
        # If there's no logged-in user or no ID, do nothing or raise error
        return

    message = message.replace("|", "")  # Remove pipe symbol
    r = request.app[REDIS_KEY]

    # Use a specific key for toast flashes to match WebSocket consumption
    # and avoid race conditions with legacy flash_middleware
    key = f"flash_toast_{user.id}"
    channel_name = f"user_{user.id}"

    msg_data = {
        "type": "flash",
        "mid": str(uuid.uuid4()),
        "body": message,
        "category": category,
        "created_at": datetime.now().isoformat(),
    }

    payload = json.dumps(msg_data)

    # Push to list for persistence (redirects)
    await r.rpush(key, payload)
    await r.expire(key, 60)  # Keep for 60 seconds (enough for redirect)

    # Publish for realtime
    await r.publish(channel_name, payload)


async def admin_event(event_name: str, request) -> None:
    db = request.get("db")
    if db is None:
        return

    user = request.get("user")
    user_id = getattr(user, "id", None)
    user_email = getattr(user, "email", None) or "anonymous"
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip_address = (
        forwarded_for.split(",", 1)[0].strip() if forwarded_for else request.remote
    ) or None

    from vchat.models import AdminEvent

    normalized_event_name = (event_name or "").strip()[:128] or "unknown_event"

    page_value = ""
    referer = (request.headers.get("Referer") or "").strip()
    if referer:
        if referer.startswith("/"):
            page_value = referer
        else:
            try:
                referer_url = URL(referer)
                page_value = referer_url.path_qs or referer_url.path or ""
            except ValueError:
                page_value = ""
    if not page_value:
        page_value = str(
            getattr(request, "path_qs", "") or getattr(request, "path", "") or ""
        ).strip()

    if page_value and page_value != "/":
        normalized_event_name = f"{normalized_event_name} @ {page_value}"[:128]

    db.add(
        AdminEvent(
            user_id=user_id,
            user_email=user_email,
            ip_address=ip_address,
            event_name=normalized_event_name,
        )
    )
    await db.commit()


serializer_timed = URLSafeTimedSerializer(config["secret_key"])
serializer = URLSafeSerializer(config["secret_key"])


def protect(value, salt=None):
    """
    Protects the value with itsdangerous.URLSafeSerializer
    """
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return serializer.dumps(value, salt)


def protect_timed(value, salt=None):
    """
    Protects the value with itsdangerous.URLSafeTimedSerializer
    """
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return serializer_timed.dumps(value, salt)


class DummyJar(AbstractCookieJar):
    def __init__(self, loop=None):
        super().__init__(loop=loop)

    def update_cookies(self, cookies, response_url=None):
        pass

    def filter_cookies(self, request_url):
        return None

    def clear_domain(self, domain):
        pass

    def __iter__(self):
        raise StopIteration

    def __len__(self):
        return 0

    def clear(self, predicate=None):
        return


def make_full_url(request, endpoint, **kwargs):
    """
    Is used to generate full url for external usage
    by using public_url from config as host
    Use it like this: {{ external('endpoint') }}
    It will return "{public_url}/{endpoint}"
    """
    queries = kwargs.pop("query_", False)
    kwargs = {k: str(v) for k, v in kwargs.items()}
    return URL(
        URL(request.app[CONFIG_KEY].get("public_url", "/"))
        / (
            str(
                request.app.router[endpoint].url_for(**kwargs).with_query(**queries)
            ).lstrip("/")
            if queries
            else str(request.app.router[endpoint].url_for(**kwargs)).lstrip("/")
        )
    )


def login_required():
    def decorator(func):
        @wraps(func)
        async def decorated_view(request):
            request["auth_session"] = await get_session(request)
            if not request["auth_session"] or request.get("user", None) is None:
                url = request.app.router["login"].url_for()
                if request.path != url:
                    query_params = {"next": request.path}
                    url = f"{url}?{urlencode(query_params)}"
                raise web.HTTPFound(url)

            return await func(request)

        return decorated_view

    return decorator


def validate_signed_user_csrf(request) -> None:
    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    try:
        signed_user_id = request.app[SIGNER_KEY].loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    if signed_user_id != request["user"].id:
        raise web.HTTPForbidden(text="Invalid CSRF Token Owner")


def validate_signed_chat_csrf(request) -> str:
    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    try:
        payload = request.app[SIGNER_KEY].loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    if not isinstance(payload, dict) or not payload.get("chat_id"):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    return str(payload["chat_id"])


def htmx_required(
    *,
    actions: set[str] | None = None,
    exempt_actions: set[str] | None = None,
    payload: str = "user",
):
    exempt_actions = exempt_actions or set()

    def decorator(func):
        @wraps(func)
        async def decorated_view(request):
            action = request.match_info.get("action")
            if actions is not None and not action:
                data = await request.post()
                action = (data.get("action") or "").strip()

            if actions is not None and action not in actions:
                return await func(request)
            if action in exempt_actions:
                return await func(request)

            if payload == "chat":
                request["csrf_chat_id"] = validate_signed_chat_csrf(request)
            elif payload == "user":
                validate_signed_user_csrf(request)
            else:
                raise RuntimeError(f"Unsupported HTMX CSRF payload: {payload}")

            return await func(request)

        return decorated_view

    return decorator


def paginator(
    total: int,
    *,
    page: int = 1,
    per_page: int = 10,
    query_factory: Callable[[int], dict[str, str] | None] | None = None,
    href_factory: Callable[[int], str] | None = None,
) -> dict:
    total = max(int(total), 0)
    per_page = max(int(per_page), 1)
    total_pages = ceil(total / per_page) if total else 0
    if total_pages:
        page = min(max(int(page), 1), total_pages)
    else:
        page = 1

    has_prev = total_pages > 0 and page > 1
    has_next = total_pages > 0 and page < total_pages

    page_numbers: list[int | None]
    if total_pages <= 7 and total_pages > 0:
        page_numbers = list(range(1, total_pages + 1))
    elif total_pages > 0:
        page_numbers = [1]
        if page - 2 > 2:
            page_numbers.append(None)
        for number in range(max(2, page - 2), min(total_pages - 1, page + 2) + 1):
            page_numbers.append(number)
        if total_pages - (page + 2) > 1:
            page_numbers.append(None)
        if total_pages > 1:
            page_numbers.append(total_pages)
    else:
        page_numbers = []

    pages: list[dict] = []
    for number in page_numbers:
        if number is None:
            pages.append({"number": None})
            continue
        item: dict[str, object] = {"number": number, "is_current": number == page}
        if query_factory is not None:
            item["query"] = query_factory(number)
        if href_factory is not None:
            item["href"] = href_factory(number)
        pages.append(item)

    range_start = ((page - 1) * per_page) + 1 if total else 0
    range_end = min(page * per_page, total) if total else 0

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_query": query_factory(page - 1) if query_factory and has_prev else None,
        "next_query": query_factory(page + 1) if query_factory and has_next else None,
        "prev_href": href_factory(page - 1) if href_factory and has_prev else None,
        "next_href": href_factory(page + 1) if href_factory and has_next else None,
        "pages": pages,
        "range_start": range_start,
        "range_end": range_end,
    }


async def run_command(command):
    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def get_all_users(db_session):
    from vchat.models import User

    users = await db_session.execute(sa.select(User).where(User.is_active.is_(True)))
    return users.scalars().fetchall()


md_extensions = [
    "markdown.extensions.admonition",
    "markdown.extensions.footnotes",
    "pymdownx.tasklist",
    "pymdownx.highlight",
    "markdown.extensions.meta",
    "markdown.extensions.tables",
    "pymdownx.details",
    "pymdownx.keys",
    "pymdownx.magiclink",
    "pymdownx.smartsymbols",
    "pymdownx.striphtml",
]
md_config = {
    "pymdownx.highlight": {"linenums": False},
    "pymdownx.keys": {
        "separator": "＋",
        "key_map": {"osx-del": "Delete", "osx-return": "Return"},
    },
}

md = markdown.Markdown(
    extensions=md_extensions, extension_configs=md_config, output_format="html5"
)


def convert_to_html(text: str) -> Tuple[str, dict]:
    """Convert markdown to html and return metadata"""
    return md.convert(text), {**getattr(md, "Meta", {})}


# --- Redis (optional) support for background tasks ---
redis = aioredis.from_url(REDIS_URL, decode_responses=True)


async def run_task(task: str, queue: str | None = None, **kwargs) -> str:
    """
    Push a background task description into Redis list queue.
    A minimal, portable format that can be consumed by any worker process.
    Returns the task id.
    """

    queue_name = queue or CELERY_DEFAULT_QUEUE

    # Celery/kombu envelope
    task_id = str(uuid.uuid4())
    body_list = [
        [],  # args (we use kwargs only)
        kwargs or {},  # kwargs
        {  # embed
            "callbacks": None,
            "errbacks": None,
            "chain": None,
            "chord": None,
        },
    ]
    body_json = json.dumps(body_list, ensure_ascii=False).encode("utf-8")
    body_b64 = base64.b64encode(body_json).decode("ascii")
    task_headers = {
        "lang": "py",
        "task": task,
        "id": task_id,
        "argsrepr": "()",
        "kwargsrepr": json.dumps(kwargs, ensure_ascii=False),
        "origin": os.uname().nodename if hasattr(os, "uname") else "vchat",
        "ignore_result": False,
        "retries": 0,
        "timelimit": [None, None],
        "root_id": task_id,
        "parent_id": None,
        "group": None,
        "group_index": None,
        "replaced_task_nesting": 0,
        "stamped_headers": None,
        "stamps": {},
        "eta": None,
        "expires": None,
        "shadow": None,
    }
    request_id = get_request_id()
    if request_id:
        task_headers[REQUEST_ID_HEADER.lower()] = request_id

    envelope = {
        "body": body_b64,
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": task_headers,
        "properties": {
            "correlation_id": task_id,
            "reply_to": str(uuid.uuid4()),
            "delivery_mode": 2,
            "delivery_info": {"exchange": "", "routing_key": queue_name},
            "priority": 0,
            "body_encoding": "base64",
            "delivery_tag": str(uuid.uuid4()),
        },
    }

    payload = json.dumps(envelope, ensure_ascii=False)

    print(f"Enqueue task {task}: {payload}")
    await redis.lpush(queue_name, payload)
    return task_id
