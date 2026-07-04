import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import sqlalchemy as sa
from aiohttp import web
from cryptography.fernet import Fernet
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from jobs.crawler.tasks import crawl_page_task
from vchat.settings import REDIS_KEY, cfg
from vchat.utils import json_response
from vchat.models import ApiClient, Page, Source
from vchat.models.data import api_client_source
from vchat.views.projects.page_status import PageStatus

__all__ = [
    "update_document",
]

MAX_URL_LENGTH = 2048
MAX_CLIENT_ID_LENGTH = 64
SIGNATURE_LENGTH = 64
MAX_NONCE_LENGTH = 128


class UpdatePayload(BaseModel):
    url: str
    client_id: str
    timestamp: str
    nonce: str
    signature: str

    @model_validator(mode="before")
    @classmethod
    def normalize_form_fields(cls, data: Any) -> dict[str, str]:
        source = data if isinstance(data, dict) else {}
        return {
            "url": str(source.get("url") or "").strip(),
            "client_id": str(source.get("client_id") or "").strip(),
            "timestamp": str(source.get("timestamp") or "").strip(),
            "nonce": str(source.get("nonce") or "").strip(),
            "signature": str(source.get("signature") or "").strip(),
        }

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value:
            raise ValueError("missing")
        if len(value) > MAX_URL_LENGTH:
            raise ValueError("too_long")
        return value

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        if not value:
            raise ValueError("missing")
        if len(value) > MAX_CLIENT_ID_LENGTH:
            raise ValueError("too_long")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        if not value:
            raise ValueError("missing")
        try:
            timestamp = int(value)
        except ValueError:
            raise ValueError("invalid") from None

        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - timestamp) > cfg.api_update_timestamp_ttl_seconds:
            raise ValueError("expired")
        return value

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        if not value:
            raise ValueError("missing")
        if len(value) > MAX_NONCE_LENGTH:
            raise ValueError("too_long")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        if not value:
            raise ValueError("missing")
        if len(value) != SIGNATURE_LENGTH:
            raise ValueError("invalid")
        return value


def _error(message: str, status: int = 400) -> web.Response:
    return json_response({"status": "error", "message": message}, status=status)


def _fernet_key(key: bytes | str) -> bytes | str:
    if isinstance(key, str):
        key = key.encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(key).digest())


def encrypt_client_secret(secret: str, key: bytes | str) -> str:
    return Fernet(_fernet_key(key)).encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_client_secret(encrypted_secret: str, key: bytes | str) -> str:
    return (
        Fernet(_fernet_key(key))
        .decrypt(encrypted_secret.encode("utf-8"))
        .decode("utf-8")
    )


def build_update_signature_payload(
    *,
    url: str,
    client_id: str,
    timestamp: str,
    nonce: str,
) -> str:
    data = {
        "url": url,
        "client_id": client_id,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    return "\n".join(f"{key}={value}" for key, value in sorted(data.items()))


def sign_update_request(
    secret: str,
    *,
    url: str,
    client_id: str,
    timestamp: str,
    nonce: str,
) -> str:
    payload = build_update_signature_payload(
        url=url,
        client_id=client_id,
        timestamp=timestamp,
        nonce=nonce,
    )
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


async def _read_update_payload(request: web.Request) -> dict[str, str]:
    if request.content_type != "application/x-www-form-urlencoded":
        raise web.HTTPUnsupportedMediaType(text="Use application/x-www-form-urlencoded")

    raw = await request.post()

    data = {
        "url": str(raw.get("url") or "").strip(),
        "client_id": str(raw.get("client_id") or "").strip(),
        "timestamp": str(raw.get("timestamp") or "").strip(),
        "nonce": str(raw.get("nonce") or "").strip(),
        "signature": str(raw.get("signature") or "").strip(),
    }
    return data


async def _check_update_rate_limit(request: web.Request, client_id: str) -> bool:
    redis = request.app[REDIS_KEY]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    key = f"api_update:rate:{client_id}"
    member = f"{now_ms}:{secrets.token_hex(8)}"
    allowed: str = await redis.eval(  # type: ignore
        """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        local limit = tonumber(ARGV[3])
        local member = ARGV[4]
        redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
        local count = redis.call('ZCARD', key)
        if count >= limit then
            redis.call('EXPIRE', key, math.ceil(window / 1000))
            return 0
        end
        redis.call('ZADD', key, now, member)
        redis.call('EXPIRE', key, math.ceil(window / 1000))
        return 1
        """,
        1,
        key,
        now_ms,
        cfg.api_update_rate_limit_window_seconds * 1000,
        cfg.api_update_rate_limit_requests,
        member,
    )
    return bool(allowed)


async def _claim_update_nonce(
    request: web.Request,
    client_id: str,
    nonce: str,
) -> bool:
    ttl = cfg.api_update_nonce_ttl_seconds
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    key = f"api_update:nonce:{client_id}:{nonce_hash}"
    redis = request.app[REDIS_KEY]
    claimed: Any = await redis.set(key, "1", ex=ttl, nx=True)
    return bool(claimed)


async def _authenticate_update_request(
    request: web.Request,
    data: dict[str, str],
) -> ApiClient | web.Response:
    try:
        payload = UpdatePayload.model_validate(data)
    except ValidationError as exc:
        errors = []
        for error in exc.errors():
            ctx_error = error.get("ctx", {}).get("error")
            reason = str(ctx_error) if ctx_error else str(error["msg"])
            errors.append(
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "reason": reason,
                }
            )
        return json_response(
            {"status": "error", "message": "Validation error", "errors": errors},
            status=400,
        )

    client = await request["db"].scalar(
        sa.select(ApiClient).where(ApiClient.client_id == payload.client_id)
    )
    if client is None or not client.is_active:
        return _error("Invalid client", status=401)

    secret = decrypt_client_secret(
        client.encrypted_secret,
        cfg.secret_key,
    )
    expected = sign_update_request(
        secret,
        url=payload.url,
        client_id=payload.client_id,
        timestamp=payload.timestamp,
        nonce=payload.nonce,
    )
    if not hmac.compare_digest(expected, payload.signature):
        return _error("Invalid signature", status=401)

    if not await _claim_update_nonce(request, payload.client_id, payload.nonce):
        return _error("Nonce has already been used", status=401)

    if not await _check_update_rate_limit(request, payload.client_id):
        return _error("Rate limit exceeded", status=429)

    return client


def _normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def _extract_host(value: str) -> str | None:
    parsed = urlparse(value)
    return _normalize_host(parsed.hostname) if parsed.hostname else None


def _is_host_allowed(host: str, source_hosts: set[str]) -> bool:
    host = _normalize_host(host)
    if host in source_hosts:
        return True
    return any(host.endswith(f".{allowed}") for allowed in source_hosts)


async def _get_source_hosts(
    request: web.Request,
    client: ApiClient,
) -> list[tuple[int, str]]:
    rows = (
        await request["db"].execute(
            sa.select(Source.id, Source.uri)
            .join(api_client_source, api_client_source.c.source_id == Source.id)
            .where(api_client_source.c.api_client_id == client.id)
        )
    ).all()

    source_hosts: list[tuple[int, str]] = []
    for source_id, source_uri in rows:
        host = _extract_host(source_uri)
        if host:
            source_hosts.append((source_id, host))
    return source_hosts


async def update_document(request: web.Request) -> web.Response:
    """
    Update or remove a page in the index.
    ---
    summary: Update indexed document by URL
    description: The authenticated API client can update only URLs that match its selected sources.
    tags:
      - public-api
    requestBody:
      required: true
      content:
        application/x-www-form-urlencoded:
          schema:
            type: object
            required:
              - url
              - client_id
              - timestamp
              - nonce
              - signature
            properties:
              url:
                type: string
                format: uri
                maxLength: 2048
                example: https://example.com/docs/page
              client_id:
                type: string
                maxLength: 64
                example: vchatid-0123456789abcdef
              timestamp:
                type: string
                description: Unix timestamp in seconds. Must be within 60 seconds of server time.
                example: "1780640000"
              nonce:
                type: string
                maxLength: 128
                description: Unique random value for this request.
                example: "4aa67162c8d24f3aa8c37df642885a65"
              signature:
                type: string
                maxLength: 64
                minLength: 64
                description: HMAC-SHA256 hex digest over sorted form fields except signature, joined as key=value lines.
                example: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    responses:
      '202':
        description: Document update task queued.
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: ok
                action:
                  type: string
                  enum: [queued]
                url:
                  type: string
                final_url:
                  type: string
                  nullable: true
                message:
                  type: string
      '400':
        description: Invalid request body or URL.
      '401':
        description: Invalid client, signature, timestamp, or nonce.
      '429':
        description: Rate limit exceeded.
    """
    data = await _read_update_payload(request)
    auth_result = await _authenticate_update_request(request, data)
    if isinstance(auth_result, web.Response):
        return auth_result

    url = data["url"]
    if not url:
        return _error("Missing field: url", status=400)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _error("Invalid URL", status=400)

    source_rows = await _get_source_hosts(request, auth_result)
    source_hosts = {host for _, host in source_rows}
    if not _is_host_allowed(parsed.hostname, source_hosts):
        return _error("Domain is not allowed", status=403)

    host = _normalize_host(parsed.hostname)
    source_id = None
    for candidate_source_id, source_host in source_rows:
        if host == source_host:
            source_id = candidate_source_id
            break
    if source_id is None:
        for candidate_source_id, source_host in source_rows:
            if host.endswith(f".{source_host}"):
                source_id = candidate_source_id
                break
    if source_id is None:
        return _error("Domain is not allowed", status=403)

    db = request["db"]
    page = await db.scalar(sa.select(Page).where(Page.uri == url))

    if page is None:
        page = Page(
            source_id=source_id,
            uri=url,
            status=PageStatus.crawler,
            status_error=None,
            discover_by="api",
            discover_source=auth_result.client_id,
        )
        page._hash = ""
        db.add(page)
    else:
        page.source_id = source_id
        page.status = PageStatus.crawler
        page.status_error = None
        if not page.discover_by:
            page.discover_by = "api"
        page.discover_source = auth_result.client_id
        page.updated_at = datetime.now(timezone.utc)

    page.patch_meta(
        remove=("error", "message", "reason", "exception_class"),
        force_reprocess_once=True,
    )
    await db.flush()
    await db.commit()
    crawl_page_task.delay(page.id)

    return json_response(
        {
            "status": "ok",
            "action": "queued",
            "url": url,
            "final_url": None,
            "message": "Document update queued",
        },
        status=202,
    )
