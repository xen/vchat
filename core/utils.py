import asyncio
import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
import aiohttp_jinja2
import html2text
import markdown
import msgspec
import redis.asyncio as aioredis
import sqlalchemy as sa
from aiohttp.abc import AbstractCookieJar, AbstractView
from aiohttp_session import get_session
from itsdangerous import (
    URLSafeSerializer,
    URLSafeTimedSerializer,
)
from markupsafe import Markup
from yarl import URL

from core.aiohttp_client import client_session
from core.app_keys import CONFIG_KEY, REDIS_KEY
from core.i18n import lazy_gettext as _
from core.settings import config


class _MsgSpecJSON:
    # from performance tips this recommendation
    _encoder = msgspec.json.Encoder()
    _decoder = msgspec.json.Decoder()

    def dumps(self, obj):
        # Encode to bytes, then decode to UTF-8 string
        return self._encoder.encode(obj).decode("utf-8")

    def dump(self, obj, fp):
        # Write the UTF-8 JSON string into a file-like object
        fp.write(self.dumps(obj))

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


REDIS_URL = config.get("redis_uri")
CELERY_DEFAULT_QUEUE = config.get("celery_default_queue", "embeddings")

DELAY_PROTECTION = 5

logger = logging.getLogger()


def to_str(item: Optional[str]) -> str:
    if isinstance(item, str):
        return item
    elif isinstance(item, list):
        return "".join(item)
    return str(item)


class Meta:
    title = _("Title")
    author = "core"
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

    def clear(self, predicate):
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


def gravatar_url(
    email: str, size: int = 100, default: str = "identicon", rating: str = "g"
) -> str:
    if not email:
        return ""
    email_encoded = email.strip().lower().encode("utf-8")
    email_hash = hashlib.md5(email_encoded).hexdigest()
    query_params = urlencode({"s": str(size), "d": default, "r": rating})
    return f"https://www.gravatar.com/avatar/{email_hash}?{query_params}"


def login_required(allowed_roles=None):
    def decorator(func):
        @wraps(func)
        async def decorated_view(request):
            request["auth_session"] = await get_session(request)
            if not request["auth_session"] or request.get("user", None) is None:
                url = request.app.router["login"].url_for()
                if request.path != url:
                    query_params = {"next": request.path}
                    url = f"{url}?{urlencode(query_params)}"
                return aiohttp.web.HTTPFound(url)

            if allowed_roles is not None:
                user_role = getattr(request.get("user", {}), "role", None)
                if user_role.value not in allowed_roles:
                    raise aiohttp.web.HTTPForbidden()

            return await func(request)

        return decorated_view

    return decorator


async def turnstile_validator(code=None, ip=None):
    # https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
    validate_url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    if config.get("mode", "production") == "stage":
        return True
    data = {
        "secret": config["turnstile_secret_key"],
        "response": code,
        "remoteip": ip,
    }
    async with (
        client_session(
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session,
        session.post(validate_url, data=data) as response,
    ):
        if response.ok:
            resp = await response.json()
            if resp.get("success"):
                return True

        logger.error(
            "Error request cloudflare: [%s] %s", response.status, await response.text()
        )

    return False


# confir for text generated for email
html2text.config.BODY_WIDTH = 80
html2text.config.INLINE_LINKS = True
html2text.config.PROTECT_LINKS = True


async def sendmessage(
    *, to=None, subject=None, template=None, request=None, context=None
):
    if isinstance(to, str):
        to = [to]
    async with client_session(client_timeout=5) as session:
        html = aiohttp_jinja2.render_string(template, request, context)
        text_maker = html2text.HTML2Text()
        text = text_maker.handle(html)
        data = {
            "from": "core <no-reply@mg.core.com>",
            "to": to,
            "subject": str(subject),
            "text": text,
            "html": html,
        }
        auth = aiohttp.BasicAuth("api", request.app[CONFIG_KEY]["mailgun_key"])
        base_url = request.app[CONFIG_KEY]["mailgun_url"]
        async with session.post(f"{base_url}/messages", auth=auth, data=data) as resp:
            if resp.status != 200:
                resp_text = await resp.text()
                logging.error("Error sending email: %s", resp_text)
                return False
    return True


def paginator(total: int, request: aiohttp.web_request.Request) -> str:
    page_keys = ["offset", "limit"]
    _get = request.query.get
    limit = int(_get("limit", "10")) if _get("limit", "10").isnumeric() else 10
    offset = int(_get("offset", "0")) if _get("offset", "0").isnumeric() else 0
    if total < limit:
        max_offset = 0
    else:
        max_offset = (total // limit) * limit if total % limit != 0 else total - limit
    offset_set = {0, int(max_offset)}
    offset_set.update(
        [
            o
            for o in range(offset - 2 * limit, offset + 3 * limit, limit)
            if o >= 0 and o <= max_offset
        ],
    )
    offset_set = list(offset_set)
    offset_set.sort()
    params = [f"limit={limit}"]
    for key, value in request.query.items():
        if key not in page_keys:
            params.append(f"{key}={value}")
    link = request.path
    params = "&".join(params)
    link_list = []
    for item in offset_set:
        if item == offset:
            link_list.append({"name": int(item / limit) + 1, "link": ""})
        else:
            if max_offset - limit not in offset_set and item == max_offset:
                link_list.append({"name": "...", "link": "#"})
            link_list.append(
                {
                    "name": int(item / limit) + 1,
                    "link": f"{link}?offset={item}&{params}",
                },
            )
            if limit not in offset_set and item == 0:
                link_list.append({"name": "...", "link": "#"})

    paginator_item = {
        "next_page": (
            f"{link}?offset={offset + limit}&{params}" if (offset < max_offset) else "#"
        ),
        "prev_page": (
            f"{link}?offset={offset - limit}&{params}" if (offset >= limit) else "#"
        ),
        "from_pos": int(offset + 1),
        "to_pos": int(offset + limit) if offset + limit < total else total,
        "total_pos": total,
        "link_list": link_list,
    }

    if paginator_item["next_page"] == paginator_item["prev_page"]:
        return ""

    return Markup(
        aiohttp_jinja2.render_string(
            "paginator.html",
            request,
            {"paginator": paginator_item},
        ),
    )


async def run_command(command):
    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def get_all_users(db_session):
    from core.models import User, UserRole

    users = await db_session.execute(
        sa.select(User).where(
            sa.and_(
                User.is_active.is_(True),
                User.role == UserRole.user,
            )
        )
    )
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


async def register_user(
    *,
    request,
    name,
    email,
    encrypted_password,
    active=False,
):
    from core.models import User, UserRole

    insert_query = (
        sa.insert(User)
        .values(
            name=name,
            email=email,
            password=encrypted_password,
            is_active=active,
            role=UserRole.user,
        )
        .returning(User.id)
    )
    user_id = await request["db"].execute(insert_query)
    await request["db"].commit()
    user_id = user_id.scalar()
    context = {
        "url": "https://{host}/auth/confirm/{secret}".format(
            host=request.host,
            secret=URLSafeSerializer(config["secret_key"]).dumps(email),
        )
    }
    await sendmessage(
        to=email,
        subject=_("Registration confirmation"),
        template="mail/register-email.html",
        request=request,
        context=context,
    )
    return user_id


# --- Redis (optional) support for background tasks ---
redis = aioredis.from_url(REDIS_URL, decode_responses=True)


async def run_task(task: str, **kwargs) -> int:
    """
    Push a background task description into Redis list queue.
    A minimal, portable format that can be consumed by any worker process.
    Returns the new queue length.
    """

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

    envelope = {
        "body": body_b64,
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {
            "lang": "py",
            "task": task,
            "id": task_id,
            "argsrepr": "()",
            "kwargsrepr": json.dumps(kwargs, ensure_ascii=False),
            "origin": os.uname().nodename if hasattr(os, "uname") else "core",
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
        },
        "properties": {
            "correlation_id": task_id,
            "reply_to": str(uuid.uuid4()),
            "delivery_mode": 2,
            "delivery_info": {"exchange": "", "routing_key": CELERY_DEFAULT_QUEUE},
            "priority": 0,
            "body_encoding": "base64",
            "delivery_tag": str(uuid.uuid4()),
        },
    }

    payload = json.dumps(envelope, ensure_ascii=False)

    print(f"Enqueue task {task}: {payload}")
    await redis.lpush(CELERY_DEFAULT_QUEUE, payload)
    return task_id


def save_upload_sync(file_path, content):
    with open(file_path, "wb") as f:
        f.write(content)


async def save_upload(field_storage, folder="uploads"):
    """
    Save uploaded file from aiohttp request
    """
    filename = field_storage.filename
    if not filename:
        return None

    # Generate unique name
    ext = os.path.splitext(filename)[1]
    new_name = f"{uuid.uuid4()}{ext}"

    # Base static path (assumed relative to this file: ../static)
    base_path = Path(__file__).parent.parent / "media" / folder
    if not base_path.exists():
        base_path.mkdir(parents=True, exist_ok=True)

    file_path = base_path / new_name

    content = field_storage.file.read()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, save_upload_sync, file_path, content)

    return {
        "name": filename,
        "url": f"/static/{folder}/{new_name}",
        "type": field_storage.content_type,
    }
