import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import sqlalchemy as sa
from aiohttp import ClientSession, ClientTimeout, web
from cryptography.fernet import Fernet

from vchat.app_keys import CONFIG_KEY, REDIS_KEY
from jobs.crawler.tasks import async_update_page_shingles, schedule_index_document
from jobs.crawler.document_pipeline import extract_url_document
from jobs.indexing.documents import (
    async_document_has_chunks,
    document_content_effectively_unchanged,
    raw_content_payload,
)
from vchat.document_content import document_too_big_message, is_document_too_big
from vchat.document_types import guess_document_type
from vchat.json_response import json_response
from vchat.models import ApiClient, Chunk, Page, Source
from vchat.models.data import api_client_source
from vchat.page_status import PageStatus, PageStatusError

__all__ = [
    "update_document",
]

MAX_URL_LENGTH = 2048
MAX_CLIENT_ID_LENGTH = 64
SIGNATURE_LENGTH = 64
MAX_NONCE_LENGTH = 128


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


def _validate_update_payload(
    request: web.Request,
    data: dict[str, str],
) -> web.Response | None:
    for field in ("url", "client_id", "timestamp", "nonce", "signature"):
        if not data[field]:
            return _error(f"Missing field: {field}", status=400)

    if len(data["url"]) > MAX_URL_LENGTH:
        return _error("Field is too long: url", status=400)
    if len(data["client_id"]) > MAX_CLIENT_ID_LENGTH:
        return _error("Field is too long: client_id", status=400)
    if len(data["signature"]) != SIGNATURE_LENGTH:
        return _error("Invalid signature", status=401)
    if len(data["nonce"]) > MAX_NONCE_LENGTH:
        return _error("Field is too long: nonce", status=400)

    try:
        timestamp = int(data["timestamp"])
    except ValueError:
        return _error("Invalid timestamp", status=400)

    now = int(datetime.now(timezone.utc).timestamp())
    ttl = int(request.app[CONFIG_KEY].get("api_update_timestamp_ttl_seconds", 60))
    if abs(now - timestamp) > ttl:
        return _error("Timestamp is too old", status=401)

    return None


async def _check_update_rate_limit(request: web.Request, client_id: str) -> bool:
    config = request.app[CONFIG_KEY]
    limit = int(config.get("api_update_rate_limit_requests", 60))
    window = int(config.get("api_update_rate_limit_window_seconds", 60))
    redis = request.app[REDIS_KEY]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    key = f"api_update:rate:{client_id}"
    member = f"{now_ms}:{secrets.token_hex(8)}"
    allowed = await redis.eval(
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
        window * 1000,
        limit,
        member,
    )
    return bool(allowed)


async def _claim_update_nonce(
    request: web.Request,
    client_id: str,
    nonce: str,
) -> bool:
    ttl = int(request.app[CONFIG_KEY].get("api_update_nonce_ttl_seconds", 180))
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    key = f"api_update:nonce:{client_id}:{nonce_hash}"
    redis = request.app[REDIS_KEY]
    return bool(await redis.set(key, "1", ex=ttl, nx=True))


async def _authenticate_update_request(
    request: web.Request,
    data: dict[str, str],
) -> ApiClient | web.Response:
    validation_error = _validate_update_payload(request, data)
    if validation_error is not None:
        return validation_error

    client = await request["db"].scalar(
        sa.select(ApiClient).where(ApiClient.client_id == data["client_id"])
    )
    if client is None or not client.is_active:
        return _error("Invalid client", status=401)

    secret = decrypt_client_secret(
        client.encrypted_secret,
        request.app[CONFIG_KEY]["secret_key"],
    )
    expected = sign_update_request(
        secret,
        url=data["url"],
        client_id=data["client_id"],
        timestamp=data["timestamp"],
        nonce=data["nonce"],
    )
    if not hmac.compare_digest(expected, data["signature"]):
        return _error("Invalid signature", status=401)

    if not await _claim_update_nonce(request, data["client_id"], data["nonce"]):
        return _error("Nonce has already been used", status=401)

    if not await _check_update_rate_limit(request, data["client_id"]):
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


def _pick_source_for_host(host: str, source_rows: list[tuple[int, str]]) -> int | None:
    host = _normalize_host(host)
    # Prefer exact host match, then parent-domain match.
    for source_id, source_host in source_rows:
        if host == source_host:
            return source_id
    for source_id, source_host in source_rows:
        if host.endswith(f".{source_host}"):
            return source_id
    return None


async def _fetch_url_content(
    url: str,
) -> tuple[str, str | None, bytes | None, dict]:
    timeout = ClientTimeout(total=20)
    async with ClientSession(timeout=timeout) as client:
        async with client.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            raw_body = await resp.read()
            content_type = resp.headers.get("Content-Type")
            charset = resp.charset or "utf-8"
            body = raw_body.decode(charset, errors="replace")
            return body, content_type, raw_body, dict(resp.headers)


async def _extract_content(
    url: str,
) -> tuple[str, dict[str, str], str | None, bytes | None, str | None]:
    body, content_type, raw_body, _headers = await _fetch_url_content(url)
    content, title, meta = await asyncio.to_thread(
        extract_url_document,
        url,
        html_body=body,
        content_type=content_type,
    )
    return content, dict(meta or {}), title, raw_body, content_type


async def _get_source_hosts(
    request: web.Request,
    client: ApiClient | None = None,
) -> list[tuple[int, str]]:
    if client is not None:
        rows = (
            await request["db"].execute(
                sa.select(Source.id, Source.uri)
                .join(api_client_source, api_client_source.c.source_id == Source.id)
                .where(api_client_source.c.api_client_id == client.id)
            )
        ).all()
    else:
        rows = (
            await request["db"].execute(
                sa.select(Source.id, Source.uri).where(Source.uri.isnot(None))
            )
        ).all()

    source_hosts: list[tuple[int, str]] = []
    for source_id, source_uri in rows:
        host = _extract_host(source_uri)
        if host:
            source_hosts.append((source_id, host))
    return source_hosts


async def _resolve_url_state(url: str) -> tuple[int, str | None, int]:
    """Return: (status_code, redirect_location, final_status_if_followed)."""
    timeout = ClientTimeout(total=20)
    async with ClientSession(timeout=timeout) as client:
        async with client.get(url, allow_redirects=False) as resp:
            status = resp.status
            location = resp.headers.get("Location")

        if status in {301, 302, 303, 307, 308} and location:
            redirect_url = urljoin(url, location)
            async with client.get(redirect_url, allow_redirects=True) as final_resp:
                return status, str(final_resp.url), final_resp.status

        return status, None, status


async def _delete_document_by_url(request: web.Request, url: str) -> int:
    db = request["db"]
    docs = (await db.execute(sa.select(Page).where(Page.uri == url))).scalars().all()
    if not docs:
        return 0

    deleted = 0
    for doc in docs:
        await db.execute(sa.delete(Chunk).where(Chunk.page_id == doc.id))
        await db.delete(doc)
        deleted += 1

    await db.commit()
    return deleted


async def _upsert_document(
    request: web.Request,
    source_id: int,
    url: str,
    *,
    discover_source: str | None = None,
) -> tuple[str, int]:
    db = request["db"]

    (
        content,
        meta_from_fetch,
        title,
        raw_body,
        raw_content_type,
    ) = await _extract_content(url)
    if not content:
        raise web.HTTPInternalServerError(text="Failed to extract document content")

    document = await db.scalar(sa.select(Page).where(Page.uri == url))
    created = document is None

    if document is None:
        document = Page(
            source_id=source_id,
            uri=url,
            status=PageStatus.parsing,
            discover_by="api",
            discover_source=discover_source,
        )
        db.add(document)

    effectively_unchanged = document_content_effectively_unchanged(document, content)
    has_chunks = (
        await async_document_has_chunks(db, document.id)
        if (effectively_unchanged and document.id is not None)
        else False
    )
    too_big = is_document_too_big(content)
    document.content = content
    stored_raw_content, raw_content_meta = raw_content_payload(raw_body)
    document.raw_content = stored_raw_content
    document.raw_content_size = raw_content_meta["size"]
    document.raw_content_type = raw_content_type
    document.status = PageStatus.ready if too_big else PageStatus.parsing
    document.status_error = PageStatusError.too_big if too_big else None
    document.hash_value = content
    document.language = ""
    document.length = len(content)

    meta = dict(document.meta or {})
    meta.update(meta_from_fetch)
    meta["raw_content"] = raw_content_meta
    if "doc_type" not in meta:
        guessed = guess_document_type(url, meta.get("content_type"))
        if guessed:
            meta["doc_type"] = guessed
    for key in ("error", "message", "reason", "exception_class"):
        meta.pop(key, None)
    if too_big:
        meta["reason"] = PageStatusError.too_big.value
        meta["message"] = document_too_big_message(content)
    document.meta = meta

    if title:
        document.title = title[:512]

    if too_big:
        await db.execute(sa.delete(Chunk).where(Chunk.page_id == document.id))
    elif not (effectively_unchanged and has_chunks):
        document.index_status = "queued"
    else:
        document.index_status = "indexed"

    await db.flush()
    await async_update_page_shingles(
        db,
        page_id=document.id,
        source_id=document.source_id,
        content=document.content,
    )
    await db.commit()
    await db.refresh(document)
    if not too_big and not (effectively_unchanged and has_chunks):
        schedule_index_document(document.id)

    return ("indexed" if created else "indexed", document.id)


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
      '200':
        description: Document update result.
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
                  enum: [indexed, deleted, replaced]
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
      '403':
        description: URL domain is not allowed.
      '429':
        description: Rate limit exceeded.
      '500':
        description: Source fetch failed.
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

    status, redirect_url, final_status = await _resolve_url_state(url)

    if status == 404:
        await _delete_document_by_url(request, url)
        return json_response(
            {
                "status": "ok",
                "action": "deleted",
                "url": url,
                "final_url": None,
                "message": "Page removed from index (404)",
            }
        )

    if status in {301, 302, 303, 307, 308} and redirect_url:
        redirect_host = _extract_host(redirect_url)
        if not redirect_host or not _is_host_allowed(redirect_host, source_hosts):
            return _error("Redirect target domain is not allowed", status=403)

        await _delete_document_by_url(request, url)
        if final_status == 404:
            return json_response(
                {
                    "status": "ok",
                    "action": "deleted",
                    "url": url,
                    "final_url": redirect_url,
                    "message": "Old URL removed; redirect target returns 404",
                }
            )

        source_id = _pick_source_for_host(redirect_host, source_rows)
        if source_id is None:
            return _error("No source found for redirect target domain", status=403)

        await _upsert_document(
            request,
            source_id,
            redirect_url,
            discover_source=auth_result.client_id,
        )

        return json_response(
            {
                "status": "ok",
                "action": "replaced",
                "url": url,
                "final_url": redirect_url,
                "message": "Old URL removed, final URL indexed",
            }
        )

    if status >= 400:
        return _error(f"Source returned HTTP {status}", status=500)

    source_id = _pick_source_for_host(parsed.hostname or "", source_rows)
    if source_id is None:
        return _error("No source found for domain", status=403)

    await _upsert_document(
        request,
        source_id,
        url,
        discover_source=auth_result.client_id,
    )

    return json_response(
        {
            "status": "ok",
            "action": "indexed",
            "url": url,
            "final_url": None,
            "message": "Page indexed",
        }
    )
