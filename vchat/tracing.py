from __future__ import annotations

import contextvars
import secrets
import string


REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 128
_ALLOWED_REQUEST_ID_CHARS = frozenset(string.ascii_letters + string.digits + "-_.:")

request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)


def generate_request_id() -> str:
    return secrets.token_hex(16)


def normalize_request_id(value: str | None) -> str | None:
    request_id = (value or "").strip()
    if not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
        return None
    if any(char not in _ALLOWED_REQUEST_ID_CHARS for char in request_id):
        return None
    return request_id


def get_request_id(default: str | None = None) -> str | None:
    return request_id_ctx.get(default)


def request_id_headers() -> dict[str, str]:
    request_id = request_id_ctx.get()
    return {REQUEST_ID_HEADER: request_id} if request_id else {}
