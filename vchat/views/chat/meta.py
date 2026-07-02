from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from vchat.utils import get_client_ip


def infer_device_type(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if any(token in ua for token in ("ipad", "tablet")):
        return "tablet"
    if any(token in ua for token in ("iphone", "android", "mobile")):
        return "mobile"
    return "desktop"


def infer_browser(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if "edg/" in ua:
        return "edge"
    if "opr/" in ua or "opera" in ua:
        return "opera"
    if "firefox/" in ua:
        return "firefox"
    if "chrome/" in ua and "chromium/" not in ua:
        return "chrome"
    if "safari/" in ua and "chrome/" not in ua:
        return "safari"
    if "chromium/" in ua:
        return "chromium"
    return "other"


def validate_source_page_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw or len(raw) > 2048:
        return None

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if any(ord(char) < 32 for char in raw):
        return None
    return raw


def merge_chat_meta(
    existing: dict[str, Any] | None,
    request: web.Request,
    client_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(existing or {})
    user_agent = request.headers.get("User-Agent", "").strip()
    client_meta = dict(client_meta or {})

    updates = {
        "ip_address": get_client_ip(request),
        "user_agent": user_agent or None,
        "browser": infer_browser(user_agent),
        "device_type": infer_device_type(user_agent),
        "device_fingerprint": (
            client_meta.get("device_fingerprint") or ""
        ).strip()
        or None,
        "platform": (client_meta.get("platform") or "").strip() or None,
        "language": (client_meta.get("language") or "").strip() or None,
        "timezone": (
            client_meta.get("timezone_name") or client_meta.get("timezone") or ""
        ).strip()
        or None,
        "screen": (client_meta.get("screen") or "").strip() or None,
        "source_page_url": validate_source_page_url(client_meta.get("source_page_url")),
        "session_updated_at": datetime.now(timezone.utc).isoformat(),
    }

    for key, value in updates.items():
        if value not in (None, ""):
            meta[key] = value

    return meta
