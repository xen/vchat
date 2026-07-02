from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp
import requests

from vchat.settings import config
from vchat.tracing import request_id_headers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Token:
    access_token: str
    expires_at: float


_token_cache: _Token | None = None
_token_lock = asyncio.Lock()


def _normalize_basic_auth(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("basic "):
        return raw
    return f"Basic {raw}"


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_expires_at(payload: dict[str, Any], *, now: float) -> float | None:
    # Common response patterns across OAuth-ish APIs.
    for key in ("expires_at", "expiresAt", "exp"):
        if key in payload:
            value = _coerce_float(payload.get(key))
            if value is None:
                continue
            # Heuristic: timestamps in ms since epoch are very large.
            if value > 1e12:
                value = value / 1000.0
            # If value looks like a delta (small), treat it as seconds.
            if value < 1e10 and value < now - 60:
                return now + value
            return value

    for key in ("expires_in", "expiresIn"):
        if key in payload:
            value = _coerce_float(payload.get(key))
            if value is None:
                continue
            return now + value

    return None


async def get_gigachat_access_token(
    session: aiohttp.ClientSession,
    *,
    basic_auth_key: str,
) -> str:
    """Get (and cache) an access token for GigaChat using Basic auth key.

    Uses per-process in-memory caching with a small safety margin.
    """

    global _token_cache
    now = time.time()

    margin = 30.0
    if _token_cache is not None and (_token_cache.expires_at - now) > margin:
        return _token_cache.access_token

    async with _token_lock:
        now = time.time()
        if _token_cache is not None and (_token_cache.expires_at - now) > margin:
            return _token_cache.access_token

        resolved_oauth_url = (config.get("gigachat_oauth_url") or "").strip()
        if not resolved_oauth_url:
            resolved_oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

        resolved_scope = (config.get("gigachat_scope") or "").strip()
        if not resolved_scope:
            resolved_scope = "GIGACHAT_API_PERS"

        verify_ssl_certs = bool(config.get("gigachat_verify_ssl_certs", True))
        oauth_timeout_seconds = float(config.get("gigachat_oauth_timeout_seconds", 15.0))

        auth_header = _normalize_basic_auth(basic_auth_key)
        if not auth_header:
            raise RuntimeError("Missing GigaChat authorization key (Basic)")

        headers = {
            **request_id_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": auth_header,
        }
        timeout = aiohttp.ClientTimeout(total=float(oauth_timeout_seconds))
        try:
            async with session.post(
                resolved_oauth_url,
                headers=headers,
                data={"scope": resolved_scope},
                ssl=bool(verify_ssl_certs),
                timeout=timeout,
            ) as resp:
                raw_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "GigaChat OAuth failed: status=%s url=%s body=%s",
                        resp.status,
                        resolved_oauth_url,
                        (raw_text or "").strip()[:1000],
                    )
                    raise RuntimeError(
                        f"GigaChat OAuth error {resp.status}: "
                        f"{raw_text.strip() or 'empty response'}"
                    )

                try:
                    payload = await resp.json(content_type=None)
                except Exception as exc:
                    logger.error(
                        "GigaChat OAuth returned non-JSON payload from url=%s: %s",
                        resolved_oauth_url,
                        (raw_text or "")[:1000],
                    )
                    raise RuntimeError(
                        f"GigaChat OAuth returned non-JSON response: {raw_text[:500]}"
                    ) from exc
        except asyncio.TimeoutError as exc:
            logger.error(
                "GigaChat OAuth timeout: url=%s timeout_seconds=%s",
                resolved_oauth_url,
                timeout.total,
            )
            raise RuntimeError(
                f"GigaChat OAuth timeout after {timeout.total} seconds"
            ) from exc
        except aiohttp.ClientError as exc:
            logger.exception(
                "GigaChat OAuth transport error: url=%s timeout_seconds=%s",
                resolved_oauth_url,
                timeout.total,
            )
            raise RuntimeError("GigaChat OAuth transport error") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("GigaChat OAuth returned unexpected payload")

        access_token = (
            payload.get("access_token")
            or payload.get("accessToken")
            or payload.get("token")
        )
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("GigaChat OAuth did not return access_token")

        now = time.time()
        expires_at = _parse_expires_at(payload, now=now)
        if expires_at is None:
            # Fallback: keep token for ~25 minutes to reduce refresh rate.
            expires_at = now + 25 * 60

        token = _Token(access_token=access_token.strip(), expires_at=float(expires_at))
        _token_cache = token
        logger.debug(
            "GigaChat OAuth session refreshed; expires_at=%s", token.expires_at
        )
        return token.access_token


def get_gigachat_access_token_sync(
    *,
    basic_auth_key: str,
    oauth_url: str | None = None,
    scope: str | None = None,
    verify_ssl_certs: bool | None = None,
    oauth_timeout_seconds: float | None = None,
) -> str:
    global _token_cache
    now = time.time()

    margin = 30.0
    if _token_cache is not None and (_token_cache.expires_at - now) > margin:
        return _token_cache.access_token

    resolved_oauth_url = (oauth_url or config.get("gigachat_oauth_url") or "").strip()
    if not resolved_oauth_url:
        resolved_oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    resolved_scope = (scope or config.get("gigachat_scope") or "").strip()
    if not resolved_scope:
        resolved_scope = "GIGACHAT_API_PERS"

    if verify_ssl_certs is None:
        verify_ssl_certs = bool(config.get("gigachat_verify_ssl_certs", True))

    if oauth_timeout_seconds is None:
        oauth_timeout_seconds = float(
            config.get("gigachat_oauth_timeout_seconds", 15.0)
        )

    auth_header = _normalize_basic_auth(basic_auth_key)
    if not auth_header:
        raise RuntimeError("Missing GigaChat authorization key (Basic)")

    resp = requests.post(
        resolved_oauth_url,
        headers={
            **request_id_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": auth_header,
        },
        data={"scope": resolved_scope},
        verify=bool(verify_ssl_certs),
        timeout=float(oauth_timeout_seconds),
    )
    if resp.status_code >= 400:
        logger.error(
            "GigaChat OAuth failed: status=%s url=%s body=%s",
            resp.status_code,
            resolved_oauth_url,
            (resp.text or "").strip()[:1000],
        )
        raise RuntimeError(
            f"GigaChat OAuth error {resp.status_code}: {resp.text.strip() or 'empty response'}"
        )

    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("GigaChat OAuth returned unexpected payload")

    access_token = (
        payload.get("access_token")
        or payload.get("accessToken")
        or payload.get("token")
    )
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("GigaChat OAuth did not return access_token")

    now = time.time()
    expires_at = _parse_expires_at(payload, now=now)
    if expires_at is None:
        expires_at = now + 25 * 60

    _token_cache = _Token(
        access_token=access_token.strip(),
        expires_at=float(expires_at),
    )
    return _token_cache.access_token
