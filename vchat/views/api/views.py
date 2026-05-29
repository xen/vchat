import asyncio
from urllib.parse import urljoin, urlparse

import sqlalchemy as sa
from aiohttp import ClientSession, ClientTimeout, web

from jobs.embedder.tasks import index_document
from vchat.document_pipeline import extract_url_document
from vchat.document_types import guess_document_type
from vchat.models import Chunk, Document, Source

__all__ = [
    "update_document",
]


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"status": "error", "message": message}, status=status)


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


async def _extract_content(url: str) -> tuple[str, dict[str, str], str | None]:
    content, title, meta = await asyncio.to_thread(extract_url_document, url)
    return content, dict(meta or {}), title


async def _get_source_hosts(request: web.Request) -> list[tuple[int, str]]:
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
    docs = (
        (await db.execute(sa.select(Document).where(Document.uri == url)))
        .scalars()
        .all()
    )
    if not docs:
        return 0

    deleted = 0
    for doc in docs:
        await db.execute(sa.delete(Chunk).where(Chunk.document_id == doc.id))
        await db.delete(doc)
        deleted += 1

    await db.commit()
    return deleted


async def _upsert_document(
    request: web.Request, source_id: int, url: str
) -> tuple[str, int]:
    db = request["db"]

    try:
        content, meta_from_fetch, title = await _extract_content(url)
    except Exception as exc:
        raise web.HTTPInternalServerError(
            text=f"Failed to extract document content: {exc}"
        )
    if not content:
        raise web.HTTPInternalServerError(text="Failed to extract document content")

    document = await db.scalar(
        sa.select(Document).where(Document.source_id == source_id, Document.uri == url)
    )
    created = document is None

    if document is None:
        document = Document(source_id=source_id, uri=url, status="added")
        db.add(document)

    document.content = content
    document.status = "indexed"
    document.hash_value = content
    document.language = ""
    document.length = len(content)

    meta = dict(document.meta or {})
    meta.update(meta_from_fetch)
    if "doc_type" not in meta:
        guessed = guess_document_type(url, meta.get("content_type"))
        if guessed:
            meta["doc_type"] = guessed
    document.meta = meta

    if title:
        document.title = title[:512]

    await db.commit()
    await db.refresh(document)
    index_document.delay(document.id)

    return ("indexed" if created else "indexed", document.id)


async def update_document(request: web.Request) -> web.Response:
    url = (request.query.get("url") or "").strip()
    if not url:
        return _error("Missing query parameter: url", status=400)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _error("Invalid URL", status=400)

    source_rows = await _get_source_hosts(request)
    source_hosts = {host for _, host in source_rows}
    if not _is_host_allowed(parsed.hostname, source_hosts):
        return _error("Domain is not allowed", status=403)

    try:
        status, redirect_url, final_status = await _resolve_url_state(url)
    except Exception as exc:
        return _error(f"Failed to fetch URL: {exc}", status=500)

    if status == 404:
        await _delete_document_by_url(request, url)
        return web.json_response(
            {
                "status": "ok",
                "action": "deleted",
                "url": url,
                "final_url": None,
                "message": "Document removed from index (404)",
            }
        )

    if status in {301, 302, 303, 307, 308} and redirect_url:
        redirect_host = _extract_host(redirect_url)
        if not redirect_host or not _is_host_allowed(redirect_host, source_hosts):
            return _error("Redirect target domain is not allowed", status=403)

        await _delete_document_by_url(request, url)
        if final_status == 404:
            return web.json_response(
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

        try:
            await _upsert_document(request, source_id, redirect_url)
        except web.HTTPException as exc:
            return _error(exc.text, status=500)

        return web.json_response(
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

    try:
        await _upsert_document(request, source_id, url)
    except web.HTTPException as exc:
        return _error(exc.text, status=500)

    return web.json_response(
        {
            "status": "ok",
            "action": "indexed",
            "url": url,
            "final_url": None,
            "message": "Document indexed",
        }
    )
