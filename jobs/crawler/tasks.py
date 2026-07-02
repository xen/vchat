import hashlib
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import defusedxml.ElementTree as ET
import redis
import requests
import sqlalchemy as sa

from celery.schedules import crontab
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.chunking import (
    ChunkData,
    EMBEDDING_DOCUMENT_MAX_CHARS,
    EmbedderDocumentError,
    chunk_document_text,
    count_token_ids,
    get_embed_tokenizer,
    normalize_html_document_for_chunking,
    validate_chunk_data,
)
from jobs.embedder.queue import ensure_pending_chunk_workers
from jobs.crawler.url_rules import (
    normalize_url_for_queue,
    build_source_id_by_host,
    resolve_source_id_for_url,
)
from jobs.crawler.url_rules import url_allowed_by_rules
from jobs.documents.shingles import compute_trigram_hashes, extract_content_blocks
from jobs.documents.content import (
    CHUNK_TEXT_HASH_IGNORED_CHARS,
    chunk_text_sha256,
    document_too_big_message,
    normalize_chunk_text_for_hash,
)
from vchat.models.data import Chunk, CrawlRun, Page, PageShingle, Sitemap, Source
from vchat.views.metrics import record_crawl_run
from vchat.views.projects.page_status import (
    EXCLUDED_INDEX_STATUS_ERRORS,
    PageStatus,
    PageStatusError,
)
from vchat.settings import config
from jobs.crawler.source_blocking import (
    apply_source_blocking_result,
    check_source_blocking,
)
from jobs.crawler.source_settings import (
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_REINDEX_CRON,
    is_manual_reindex,
    normalize_reindex_cron,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _advisory_lock_namespace(name: str) -> int:
    digest = hashlib.blake2s(name.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


_CRAWL_LOCK_NAMESPACE = _advisory_lock_namespace("vchat:crawl-source")
_DOCUMENT_INDEX_LOCK_NAMESPACE = _advisory_lock_namespace("vchat:document-index")
_BOILERPLATE_REBUILD_LOCK_NAMESPACE = _advisory_lock_namespace(
    "vchat:boilerplate-rebuild"
)
REDIS_URL = config.get("redis_uri", "redis://localhost:6379/0")
ENSURE_PENDING_CHUNKS_SCHEDULE_KEY = "vchat:embed:ensure_pending_chunks:scheduled"
REFRESH_PROJECT_INDEX_SCHEDULE_KEY = "vchat:embed:refresh_project_index:scheduled"
INDEX_DOCUMENT_SCHEDULE_KEY_PREFIX = "vchat:embed:index_document:scheduled:"
INDEX_CONTENT_HASH_META_KEY = "embedding_index_content_hash"
ENSURE_PENDING_CHUNKS_SCHEDULE_TTL = max(
    30, int(config.get("embedding_ensure_pending_chunks_ttl_seconds", 120) or 120)
)
REFRESH_PROJECT_INDEX_SCHEDULE_TTL = max(
    60, int(config.get("embedding_refresh_project_index_ttl_seconds", 300) or 300)
)
INDEX_DOCUMENT_SCHEDULE_TTL = max(
    300,
    int(
        config.get(
            "embedding_index_document_schedule_ttl_seconds",
            config.get("celery_visibility_timeout", 21600),
        )
        or config.get("celery_visibility_timeout", 21600)
    ),
)
METADATA_ONLY_INDEX_POLICY = "metadata_only"
INDEX_POLICY_REASON_META_KEY = "embedding_index_policy_reason"
METADATA_ONLY_CSV_MIN_CHARS = max(
    1, int(config.get("metadata_only_csv_min_chars", 50000) or 50000)
)
METADATA_ONLY_RAW_SIZE_MIN_BYTES = max(
    1, int(config.get("metadata_only_raw_size_min_bytes", 1_000_000) or 1_000_000)
)
STATISTICAL_DUMP_SAMPLE_MAX_LINES = 200
STATISTICAL_DUMP_MIN_ROWS = 50
STATISTICAL_DUMP_MIN_COLUMNS = 6
STATISTICAL_DUMP_MIN_CONSISTENT_ROW_RATIO = 0.8
STATISTICAL_DUMP_MIN_NUMERIC_CELL_RATIO = 0.55
STATISTICAL_DUMP_MAX_NATURAL_LANGUAGE_CELL_RATIO = 0.25
STATISTICAL_DUMP_DELIMITERS = (",", "\t", ";", "|")
DOWNLOADABLE_DOCUMENT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
)
DOWNLOADABLE_DOCUMENT_CONTENT_TYPE_TERMS = (
    "pdf",
    "msword",
    "powerpoint",
    "excel",
    "spreadsheet",
    "officedocument",
)
LOW_VALUE_STATUS_TITLE_PREFIXES = (
    "301 ",
    "302 ",
    "303 ",
    "307 ",
    "308 ",
    "400 ",
    "401 ",
    "403 ",
    "404 ",
    "500 ",
    "502 ",
    "503 ",
)
LOW_VALUE_STATUS_TITLE_TERMS = (
    "moved permanently",
    "moved temporarily",
    "not found",
    "forbidden",
    "unauthorized",
    "bad gateway",
    "service unavailable",
)
PAGE_SHINGLE_INSERT_BATCH_SIZE = max(
    100,
    int(config.get("page_shingle_insert_batch_size", 2000) or 2000),
)
_BOILERPLATE_HASH_CACHE: dict[int, frozenset[int]] = {}


def mark_blocked_source_pages_ready(
    session: Session,
    *,
    source_id: int,
    blocked_reason: str,
) -> None:
    session.execute(
        update(Page)
        .where(
            Page.source_id == source_id,
            Page.status == PageStatus.crawler,
            Page.status_error.is_(None),
        )
        .values(
            status=PageStatus.ready,
            status_error=(
                PageStatusError.excluded_robots
                if blocked_reason == "robots_txt"
                else blocked_reason
            ),
            last_crawled_at=datetime.now(timezone.utc),
        )
    )


@dataclass(slots=True)
class PageChunkContext:
    id: int
    source_id: int | None
    uri: str | None
    title: str | None
    content: str
    content_hash: str
    meta: dict
    status_error: PageStatusError | None
    raw_content_type: str | None
    raw_content_size: int | None


def _metadata_only_index_reason(doc: Page | PageChunkContext) -> str | None:
    meta = dict(getattr(doc, "meta", None) or {})
    uri = str(getattr(doc, "uri", "") or "")
    path = urlparse(uri).path.lower()
    doc_type = str(meta.get("doc_type") or "").lower()
    content_type = str(
        meta.get("content_type") or getattr(doc, "raw_content_type", "") or ""
    ).lower()
    content_len = len(getattr(doc, "content", "") or "")
    raw_content_size = int(getattr(doc, "raw_content_size", None) or 0)
    title = str(getattr(doc, "title", "") or "").strip()

    content = getattr(doc, "content", "") or ""
    csv_hint = (
        path.endswith((".csv", ".tsv"))
        or "csv" in content_type
        or _looks_like_statistical_dump(content)
    )
    if csv_hint and (
        content_len > METADATA_ONLY_CSV_MIN_CHARS
        or content_len > EMBEDDING_DOCUMENT_MAX_CHARS
        or raw_content_size > METADATA_ONLY_RAW_SIZE_MIN_BYTES
    ):
        return "csv_statistical_dump"

    document_hint = path.endswith(DOWNLOADABLE_DOCUMENT_EXTENSIONS) or any(
        term in content_type for term in DOWNLOADABLE_DOCUMENT_CONTENT_TYPE_TERMS
    )
    if document_hint and content_len > EMBEDDING_DOCUMENT_MAX_CHARS:
        return "large_downloadable_document"

    if "/assets/vendor/" in path or "/node_modules/" in path:
        return "vendor_asset"

    if doc_type == "code" and content_len > METADATA_ONLY_CSV_MIN_CHARS:
        return "large_code_asset"

    if (
        content_len
        and title
        and not _low_value_status_title(title)
        and not _indexable_content_for_size_policy(doc).strip()
    ):
        return "empty_visible_text"

    return None


def _looks_like_statistical_dump(content: str) -> bool:
    lines = [
        line.strip()
        for line in (content or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
        if line.strip()
    ][:STATISTICAL_DUMP_SAMPLE_MAX_LINES]
    if len(lines) < STATISTICAL_DUMP_MIN_ROWS:
        return False

    best_delimiter = ""
    best_consistent_lines: list[list[str]] = []
    for delimiter in STATISTICAL_DUMP_DELIMITERS:
        rows = [
            [cell.strip() for cell in line.split(delimiter)]
            for line in lines
            if delimiter in line
        ]
        if len(rows) < STATISTICAL_DUMP_MIN_ROWS:
            continue
        widths = [len(row) for row in rows]
        common_width, common_count = max(
            Counter(widths).items(),
            key=lambda item: item[1],
        )
        if common_width < STATISTICAL_DUMP_MIN_COLUMNS:
            continue
        consistent_rows = [row for row in rows if len(row) == common_width]
        if common_count / len(lines) < STATISTICAL_DUMP_MIN_CONSISTENT_ROW_RATIO:
            continue
        if len(consistent_rows) > len(best_consistent_lines):
            best_delimiter = delimiter
            best_consistent_lines = consistent_rows

    if not best_delimiter:
        return False

    cells = [
        cell
        for row in best_consistent_lines[:STATISTICAL_DUMP_SAMPLE_MAX_LINES]
        for cell in row
        if cell
    ]
    if not cells:
        return False

    numeric_cells = sum(1 for cell in cells if _looks_numeric_cell(cell))
    natural_language_cells = sum(
        1 for cell in cells if _looks_natural_language_cell(cell)
    )
    return (
        numeric_cells / len(cells) >= STATISTICAL_DUMP_MIN_NUMERIC_CELL_RATIO
        and natural_language_cells / len(cells)
        <= STATISTICAL_DUMP_MAX_NATURAL_LANGUAGE_CELL_RATIO
    )


def _looks_numeric_cell(cell: str) -> bool:
    normalized = cell.strip().replace(",", ".")
    if not normalized:
        return False
    if normalized in {"0", "1"}:
        return True
    try:
        float(normalized)
    except ValueError:
        return False
    return True


def _looks_natural_language_cell(cell: str) -> bool:
    words = [
        part
        for part in re.split(r"\W+", cell, flags=re.UNICODE)
        if len(part) >= 3 and any(char.isalpha() for char in part)
    ]
    return len(words) >= 3


def _low_value_status_title(title: str) -> bool:
    normalized = " ".join((title or "").lower().split())
    if not normalized:
        return False
    return normalized.startswith(LOW_VALUE_STATUS_TITLE_PREFIXES) or any(
        term in normalized for term in LOW_VALUE_STATUS_TITLE_TERMS
    )


def _metadata_only_chunk(doc: Page | PageChunkContext, reason: str) -> ChunkData:
    meta = dict(getattr(doc, "meta", None) or {})
    uri = str(getattr(doc, "uri", "") or "")
    title = str(getattr(doc, "title", "") or "").strip()
    if not title and uri:
        title = Path(urlparse(uri).path).name or uri
    doc_type = str(meta.get("doc_type") or "unknown")
    content_type = str(
        meta.get("content_type") or getattr(doc, "raw_content_type", "") or "unknown"
    )
    content = getattr(doc, "content", "") or ""
    preview = ""
    if reason != "empty_visible_text":
        preview_lines = [
            line.strip()
            for line in content[:2000]
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .split("\n")
            if line.strip()
        ]
        preview = " ".join(preview_lines[:3])[:500].strip()

    lines = [
        "Document indexed as metadata only.",
        f"Title: {title or 'Untitled document'}",
        f"URL: {uri or 'unknown'}",
        f"Document type: {doc_type}",
        f"Content type: {content_type}",
        f"Index policy reason: {reason}",
        f"Content length: {len(content)} chars",
    ]
    if preview:
        lines.append(f"Preview: {preview}")
    text = "\n".join(lines)
    return ChunkData(
        index=0,
        start=None,
        end=None,
        text=text,
        kind="file_summary",
        header_text=title or None,
        section_path=None,
        entity_terms=None,
        token_count=count_token_ids(get_embed_tokenizer(), text),
    )


def _indexable_content_for_size_policy(doc: Page | PageChunkContext) -> str:
    return normalize_html_document_for_chunking(getattr(doc, "content", "") or "")


def _document_exceeds_indexable_size_limit(doc: Page | PageChunkContext) -> bool:
    return len(_indexable_content_for_size_policy(doc)) > EMBEDDING_DOCUMENT_MAX_CHARS


def _status_error_blocks_indexing(doc: PageChunkContext) -> bool:
    if doc.status_error is None:
        return False
    if doc.status_error == PageStatusError.too_big:
        return _document_exceeds_indexable_size_limit(doc)
    return True


def fetch_page_context(session: Session, page_id: int) -> PageChunkContext | None:
    row = session.execute(
        select(
            Page.id,
            Page.source_id,
            Page.uri,
            Page.title,
            Page.content,
            Page._hash,
            Page.meta,
            Page.status_error,
            Page.raw_content_type,
            Page.raw_content_size,
        ).where(Page.id == page_id)
    ).first()
    if not row:
        logging.warning("Page %s not found", page_id)
        return None

    doc = PageChunkContext(
        id=row.id,
        source_id=row.source_id,
        uri=getattr(row, "uri", None),
        title=getattr(row, "title", None),
        content=row.content or "",
        content_hash=row._hash,
        meta=row.meta or {},
        status_error=row.status_error,
        raw_content_type=getattr(row, "raw_content_type", None),
        raw_content_size=getattr(row, "raw_content_size", None),
    )
    if not doc.content:
        logging.warning("Page %s has no content", page_id)
        return None
    if _metadata_only_index_reason(
        doc
    ) is None and _document_exceeds_indexable_size_limit(doc):
        mark_page_too_big(
            session,
            doc.id,
            content=_indexable_content_for_size_policy(doc),
        )
        return None
    if _metadata_only_index_reason(doc) is None and _status_error_blocks_indexing(doc):
        logging.info("Page %s has status_error=%s, skipping", page_id, doc.status_error)
        return None
    return doc


def load_boilerplate_hashes(session: Session, source_id: int) -> frozenset[int]:
    cached = _BOILERPLATE_HASH_CACHE.get(source_id)
    if cached is not None:
        return cached

    total: int = session.execute(
        sa.select(sa.func.count(Page.id)).where(
            Page.source_id == source_id,
            Page.content.isnot(None),
            Page.content != "",
        )
    ).scalar_one()
    if total < 5:
        return frozenset()
    rows = session.execute(
        sa.select(PageShingle.shingle_hash)
        .where(PageShingle.source_id == source_id)
        .group_by(PageShingle.shingle_hash)
        .having(sa.func.count(sa.distinct(PageShingle.page_id)) > total * 0.4)
    ).scalars()
    hashes = frozenset(rows)
    _BOILERPLATE_HASH_CACHE[source_id] = hashes
    return hashes


def page_shingle_rows(
    *,
    page_id: int,
    source_id: int,
    content: str,
) -> list[dict[str, int]]:
    page_hashes: set[int] = set()
    for block in extract_content_blocks(content):
        page_hashes.update(compute_trigram_hashes(block))
    return [
        {
            "source_id": source_id,
            "page_id": page_id,
            "shingle_hash": shingle_hash,
        }
        for shingle_hash in page_hashes
    ]


def update_page_shingles(
    session: Session,
    *,
    page_id: int,
    source_id: int | None,
    content: str | None,
) -> int:
    session.execute(sa.delete(PageShingle).where(PageShingle.page_id == page_id))
    if source_id is None or not content:
        return 0
    _BOILERPLATE_HASH_CACHE.pop(source_id, None)

    rows = page_shingle_rows(page_id=page_id, source_id=source_id, content=content)
    if rows:
        session.execute(sa.insert(PageShingle), rows)
    return len(rows)


def chunk_text_hash(text: str) -> str:
    return chunk_text_sha256(text or "")


def normalized_chunk_text_expr(column):
    return sa.func.btrim(sa.func.translate(column, CHUNK_TEXT_HASH_IGNORED_CHARS, ""))


def reuse_existing_chunk_embeddings(session: Session, *, page_id: int) -> int:
    page_chunks = list(
        session.execute(
            sa.select(Chunk)
            .where(
                Chunk.page_id == page_id,
                Chunk.embedding.is_(None),
                Chunk.is_duplicate.is_(False),
                Chunk.text_hash.isnot(None),
            )
            .order_by(Chunk.id.asc())
        )
        .scalars()
        .all()
    )
    if not page_chunks:
        return 0

    reused_count = 0
    for chunk in page_chunks:
        normalized_text = normalize_chunk_text_for_hash(chunk.text or "")
        if not chunk.text_hash or not normalized_text:
            continue
        embedding = session.execute(
            sa.select(Chunk.embedding)
            .where(
                Chunk.page_id.isnot(None),
                Chunk.id != chunk.id,
                Chunk.embedding.isnot(None),
                Chunk.is_duplicate.is_(False),
                Chunk.text_hash == chunk.text_hash,
                normalized_chunk_text_expr(Chunk.text) == normalized_text,
            )
            .order_by(Chunk.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if embedding is None:
            continue
        chunk.embedding = embedding
        reused_count += 1
    return reused_count


def mark_duplicate_page_chunks(session: Session, *, page_id: int) -> int:
    page_chunks = list(
        session.execute(
            sa.select(Chunk)
            .where(
                Chunk.page_id == page_id,
                Chunk.text_hash.isnot(None),
            )
            .order_by(Chunk.id.asc())
        )
        .scalars()
        .all()
    )
    if not page_chunks:
        return 0

    text_hashes = {chunk.text_hash for chunk in page_chunks if chunk.text_hash}
    canonical_rows = session.execute(
        sa.select(Chunk.text_hash, Chunk.id)
        .where(
            Chunk.page_id == page_id,
            Chunk.text_hash.in_(text_hashes),
            Chunk.is_duplicate.is_(False),
        )
        .order_by(Chunk.text_hash.asc(), Chunk.id.asc())
    ).all()
    canonical_by_hash: dict[str, int] = {}
    for text_hash, chunk_id in canonical_rows:
        canonical_by_hash.setdefault(text_hash, chunk_id)

    duplicate_count = 0
    for chunk in page_chunks:
        if not chunk.text_hash:
            continue
        canonical_id = canonical_by_hash.setdefault(chunk.text_hash, chunk.id)
        if chunk.id == canonical_id:
            chunk.is_duplicate = False
            chunk.duplicate_of_chunk_id = None
            continue
        chunk.is_duplicate = True
        chunk.duplicate_of_chunk_id = canonical_id
        chunk.embedding = None
        duplicate_count += 1
    return duplicate_count


async def async_update_page_shingles(
    session,
    *,
    page_id: int,
    source_id: int | None,
    content: str | None,
) -> int:
    await session.execute(sa.delete(PageShingle).where(PageShingle.page_id == page_id))
    if source_id is None or not content:
        return 0
    _BOILERPLATE_HASH_CACHE.pop(source_id, None)

    rows = page_shingle_rows(page_id=page_id, source_id=source_id, content=content)
    if rows:
        await session.execute(sa.insert(PageShingle), rows)
    return len(rows)


def _try_acquire_document_index_lock(session: Session, page_id: int) -> bool:
    result = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:namespace, :page_id)"),
        {"namespace": _DOCUMENT_INDEX_LOCK_NAMESPACE, "page_id": page_id},
    )
    return bool(result.scalar_one())


def page_chunks_match_current_content(session: Session, doc: PageChunkContext) -> bool:
    if doc.meta.get(INDEX_CONTENT_HASH_META_KEY) != doc.content_hash:
        return False
    expected_policy_reason = _metadata_only_index_reason(doc) or ""
    if doc.meta.get(INDEX_POLICY_REASON_META_KEY) != expected_policy_reason:
        return False

    chunk_count = session.execute(
        sa.select(sa.func.count(Chunk.id)).where(Chunk.page_id == doc.id)
    ).scalar_one()
    return bool(chunk_count)


def mark_page_chunks_current(session: Session, doc: Page | PageChunkContext) -> None:
    meta = dict(doc.meta or {})
    content_hash = getattr(doc, "content_hash", None) or getattr(doc, "hash_value")
    meta[INDEX_CONTENT_HASH_META_KEY] = content_hash
    meta[INDEX_POLICY_REASON_META_KEY] = _metadata_only_index_reason(doc) or ""
    doc.meta = meta


def mark_page_embedder_failed(
    session: Session,
    page_id: int,
    exc: Exception,
    *,
    message: str | None = None,
) -> None:
    page = session.get(Page, page_id)
    if page is None:
        return

    page.status = PageStatus.parsing
    page.status_error = PageStatusError.embedder_failed
    page.patch_meta(
        remove=("error", "message", "reason", "exception_class"),
        reason=PageStatusError.embedder_failed.value,
        message=message or str(exc),
        error=str(exc),
        exception_class=type(exc).__name__,
    )
    session.execute(sa.delete(Chunk).where(Chunk.page_id == page_id))
    update_page_shingles(
        session,
        page_id=page_id,
        source_id=page.source_id,
        content=None,
    )
    session.commit()


def mark_page_too_big(session: Session, page_id: int, *, content: str) -> None:
    page = session.get(Page, page_id)
    if page is None:
        return

    page.status = PageStatus.ready
    page.status_error = PageStatusError.too_big
    page.patch_meta(
        remove=("error", "message", "reason", "exception_class"),
        reason=PageStatusError.too_big.value,
        message=document_too_big_message(content),
    )
    session.execute(sa.delete(Chunk).where(Chunk.page_id == page_id))
    update_page_shingles(
        session,
        page_id=page_id,
        source_id=page.source_id,
        content=None,
    )
    session.commit()


def materialize_page_chunks(
    session: Session,
    page: Page | PageChunkContext,
    user_uid: str = "system",
) -> int:
    content = page.content or ""
    metadata_only_reason = _metadata_only_index_reason(page)
    if metadata_only_reason is None and _document_exceeds_indexable_size_limit(page):
        mark_page_too_big(
            session,
            page.id,
            content=_indexable_content_for_size_policy(page),
        )
        return 0

    boilerplate_hashes: frozenset[int] = frozenset()
    if page.source_id is not None and metadata_only_reason is None:
        boilerplate_hashes = load_boilerplate_hashes(session, page.source_id)

    if metadata_only_reason is not None:
        meta = dict(page.meta or {})
        meta["index_policy"] = METADATA_ONLY_INDEX_POLICY
        meta["index_policy_reason"] = metadata_only_reason
        page.meta = meta
        chunks = [_metadata_only_chunk(page, metadata_only_reason)]
    else:
        meta = dict(page.meta or {})
        meta.pop("index_policy", None)
        meta.pop("index_policy_reason", None)
        page.meta = meta
        chunks = chunk_document_text(
            content,
            boilerplate_hashes=boilerplate_hashes or None,
        )
    validate_chunk_data(chunks, page_id=page.id)
    logging.info("Materializing %s chunks for Page %s", len(chunks), page.id)

    session.execute(sa.delete(Chunk).where(Chunk.page_id == page.id))

    if not chunks:
        logging.info("No content to index for Page %s", page.id)
        mark_page_chunks_current(session, page)
        session.execute(
            sa.update(Page)
            .where(Page.id == page.id)
            .values(status=PageStatus.ready, status_error=None, meta=page.meta)
        )
        session.commit()
        return 0

    for chunk_data in chunks:
        session.add(
            Chunk(
                user_uid=user_uid,
                page_id=page.id,
                chunk_ix=chunk_data.index,
                start_offset=chunk_data.start,
                end_offset=chunk_data.end,
                kind=chunk_data.kind,
                header_text=chunk_data.header_text,
                section_path=chunk_data.section_path,
                entity_terms=chunk_data.entity_terms,
                token_count=chunk_data.token_count,
                text=chunk_data.text,
                text_hash=chunk_text_hash(chunk_data.text),
                is_duplicate=False,
                duplicate_of_chunk_id=None,
                embedding=None,
            )
        )
    session.flush()
    reused_embedding_count = reuse_existing_chunk_embeddings(session, page_id=page.id)
    if reused_embedding_count:
        logging.info(
            "Reused embeddings for %s chunks on Page %s by text hash",
            reused_embedding_count,
            page.id,
        )
    duplicate_count = mark_duplicate_page_chunks(session, page_id=page.id)
    if duplicate_count:
        logging.info(
            "Marked %s duplicate chunks for Page %s by text hash",
            duplicate_count,
            page.id,
        )

    mark_page_chunks_current(session, page)
    session.execute(
        sa.update(Page)
        .where(Page.id == page.id)
        .values(status=PageStatus.ready, status_error=None, meta=page.meta)
    )
    session.commit()
    session.expunge_all()
    return len(chunks)


def index_page_chunks(session: Session, page: Page | PageChunkContext) -> bool:
    chunk_count = materialize_page_chunks(session, page)
    if chunk_count == 0:
        return False
    schedule_ensure_pending_chunks()
    return True


def index_page_inner(session: Session, page_id: int) -> bool:
    context = fetch_page_context(session, page_id)
    if not context:
        return False
    if not _try_acquire_document_index_lock(session, page_id):
        logging.info("Page %s is already reserved for chunk materialization", page_id)
        session.rollback()
        return False
    if page_chunks_match_current_content(session, context):
        pending = session.execute(
            sa.select(sa.func.count(Chunk.id)).where(
                Chunk.page_id == page_id,
                Chunk.embedding.is_(None),
                Chunk.is_duplicate.is_(False),
            )
        ).scalar_one()
        if pending:
            schedule_ensure_pending_chunks()
        else:
            session.execute(
                sa.update(Page)
                .where(Page.id == page_id)
                .values(status=PageStatus.ready, status_error=None)
            )
        session.commit()
        logging.info("Page %s chunks already match current content, skipping", page_id)
        return bool(pending)
    return index_page_chunks(session, context)


def rebuild_boilerplate_for_source(session: Session, source_id: int) -> int:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :source_id)"),
        {
            "namespace": _BOILERPLATE_REBUILD_LOCK_NAMESPACE,
            "source_id": source_id,
        },
    )
    _BOILERPLATE_HASH_CACHE.pop(source_id, None)
    session.execute(sa.delete(PageShingle).where(PageShingle.source_id == source_id))

    content_result = session.execute(
        sa.select(Page.id, Page.content)
        .where(
            Page.source_id == source_id,
            Page.content.isnot(None),
            Page.content != "",
        )
        .execution_options(yield_per=100)
    )

    distinct_shingles: set[int] = set()
    page_count = 0
    batch: list[dict[str, int]] = []
    for page_id, content in content_result:
        page_count += 1
        rows = page_shingle_rows(
            page_id=page_id,
            source_id=source_id,
            content=content,
        )
        distinct_shingles.update(row["shingle_hash"] for row in rows)
        for row in rows:
            batch.append(row)
            if len(batch) >= PAGE_SHINGLE_INSERT_BATCH_SIZE:
                session.execute(sa.insert(PageShingle), batch)
                batch.clear()

    if page_count == 0:
        session.commit()
        return 0

    if batch:
        session.execute(sa.insert(PageShingle), batch)
    session.commit()
    logging.info(
        "Rebuilt boilerplate index for source %s: %s distinct shingles from %s pages",
        source_id,
        len(distinct_shingles),
        page_count,
    )
    return len(distinct_shingles)


def _try_acquire_source_crawl_lock(session: Session, source_id: int) -> bool:
    result = session.execute(
        text("SELECT pg_try_advisory_lock(:namespace, :source_id)"),
        {"namespace": _CRAWL_LOCK_NAMESPACE, "source_id": source_id},
    )
    return bool(result.scalar_one())


def _release_source_crawl_lock(session: Session, source_id: int) -> None:
    session.execute(
        text("SELECT pg_advisory_unlock(:namespace, :source_id)"),
        {"namespace": _CRAWL_LOCK_NAMESPACE, "source_id": source_id},
    )


def _find_active_crawl_run(
    session: Session,
    source_id: int,
    *,
    now: datetime,
    max_crawl_age: timedelta,
):
    return session.execute(
        select(CrawlRun).where(
            CrawlRun.source_id == source_id,
            CrawlRun.finished_at.is_(None),
            CrawlRun.started_at >= now - max_crawl_age,
        )
    ).scalar_one_or_none()


def _reserve_source_crawl_run(
    session: Session,
    source_id: int,
    *,
    now: datetime,
    max_crawl_age: timedelta = timedelta(days=30),
) -> int | None:
    if not _try_acquire_source_crawl_lock(session, source_id):
        return None

    try:
        active_run = _find_active_crawl_run(
            session,
            source_id,
            now=now,
            max_crawl_age=max_crawl_age,
        )
        if active_run:
            return None

        session.execute(
            update(CrawlRun)
            .where(
                CrawlRun.source_id == source_id,
                CrawlRun.finished_at.is_(None),
            )
            .values(
                finished_at=now,
                exit_reason="interrupted",
            )
        )

        run = CrawlRun(source_id=source_id, started_at=now)
        session.add(run)
        session.flush()
        session.commit()
        return run.id
    finally:
        _release_source_crawl_lock(session, source_id)


def _mark_crawl_run_finished(
    source_id: int,
    crawl_run_id: int | None,
    *,
    exit_reason: str,
    notes: str | None = None,
) -> None:
    if not crawl_run_id:
        return

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            run = session.get(CrawlRun, crawl_run_id)
            if not run or run.source_id != source_id:
                return
            if run.finished_at is None:
                run.finished_at = datetime.now(timezone.utc)
            run.exit_reason = exit_reason
            if notes:
                run.notes = notes
            session.commit()
    finally:
        engine.dispose()


def schedule_pending_chunk_tasks(task_count: int) -> int:
    scheduled = 0
    for _ in range(max(0, int(task_count or 0))):
        app.send_task(
            "jobs.embedder.tasks.pending_chunks",
            kwargs={"counted": True},
            queue="embeddings",
        )
        scheduled += 1
    return scheduled


def schedule_ensure_pending_chunks() -> bool:
    redis_client = redis.from_url(REDIS_URL)
    try:
        acquired = redis_client.set(
            ENSURE_PENDING_CHUNKS_SCHEDULE_KEY,
            "1",
            ex=ENSURE_PENDING_CHUNKS_SCHEDULE_TTL,
            nx=True,
        )
        if not acquired:
            return False

        ensure_pending_chunks.delay()
        return True
    finally:
        redis_client.close()


def schedule_refresh_project_index() -> bool:
    redis_client = redis.from_url(REDIS_URL)
    try:
        acquired = redis_client.set(
            REFRESH_PROJECT_INDEX_SCHEDULE_KEY,
            "1",
            ex=REFRESH_PROJECT_INDEX_SCHEDULE_TTL,
            nx=True,
        )
        if not acquired:
            return False

        refresh_project_index.delay()
        return True
    finally:
        redis_client.close()


def index_document_schedule_key(document_id: int) -> str:
    return f"{INDEX_DOCUMENT_SCHEDULE_KEY_PREFIX}{document_id}"


def schedule_index_document(document_id: int) -> bool:
    redis_client = redis.from_url(REDIS_URL)
    schedule_key = index_document_schedule_key(document_id)
    try:
        acquired = redis_client.set(
            schedule_key,
            "1",
            ex=INDEX_DOCUMENT_SCHEDULE_TTL,
            nx=True,
        )
        if not acquired:
            return False

        index_document.delay(document_id)
        return True
    finally:
        redis_client.close()


@app.task(
    name="jobs.crawler.tasks.crawl_source_task",
    queue="celery",
)
def crawl_source_task(source_id: int, skip_sitemap_sync: bool = False):
    print(f"Starting crawl for source {source_id}")

    engine = create_sync_engine()
    crawl_run_id: int | None = None
    try:
        with Session(bind=engine) as session:
            source = session.execute(
                select(Source).where(
                    Source.id == source_id,
                    Source.is_paused.is_(False),
                    Source.blocked_reason.is_(None),
                )
            ).scalar_one_or_none()
            if source is None:
                blocked_source = session.execute(
                    select(Source.id, Source.blocked_reason).where(
                        Source.id == source_id,
                        Source.is_paused.is_(False),
                        Source.blocked_reason.isnot(None),
                    )
                ).one_or_none()
                if blocked_source is not None:
                    mark_blocked_source_pages_ready(
                        session,
                        source_id=blocked_source.id,
                        blocked_reason=blocked_source.blocked_reason,
                    )
                    session.commit()
                print(f"Source {source_id} is not crawlable, skipping")
                return

            blocking_result = check_source_blocking(
                source.uri,
                ignore_robots_txt=source.config.ignore_robots_txt,
            )
            apply_source_blocking_result(source, blocking_result)
            blocked_reason = blocking_result.reason
            if blocked_reason is not None:
                mark_blocked_source_pages_ready(
                    session,
                    source_id=source_id,
                    blocked_reason=blocked_reason.value,
                )
            session.commit()
            if blocked_reason is not None:
                print(
                    f"Source {source_id} blocked before crawl: "
                    f"{blocked_reason.value}"
                )
                return

            url = source.uri
            source_title = source.title
            crawler_payload = source.config.to_dict()
            tracked_sources: list[dict] = []
            seen_hosts: set[str] = set()
            source_rows = [source]
            source_rows.extend(
                session.execute(select(Source).where(Source.is_paused.is_(False)))
                .scalars()
                .all()
            )
            for tracked_source in source_rows:
                host = (urlparse(tracked_source.uri).hostname or "").lower()
                if not host or host in seen_hosts:
                    continue
                tracked_sources.append(
                    {
                        "id": tracked_source.id,
                        "uri": tracked_source.uri,
                        "rules": list(
                            tracked_source.config.to_dict().get("rules", []) or []
                        ),
                    }
                )
                seen_hosts.add(host)
            crawler_payload["tracked_sources"] = tracked_sources
            crawl_run_id = _reserve_source_crawl_run(
                session,
                source_id,
                now=datetime.now(timezone.utc),
            )
            if crawl_run_id is None:
                print(
                    f"Source {source_id}: crawl already reserved or running, skipping"
                )
                return
            crawler_payload["crawl_run_id"] = crawl_run_id
            _refresh_source_discovery(session, source, crawler_payload)
            if not skip_sitemap_sync:
                _sync_sitemaps_for_source(session, source_id)
            session.commit()

            recent_runs = (
                session.execute(
                    select(CrawlRun)
                    .where(
                        CrawlRun.source_id == source_id,
                        CrawlRun.finished_at.is_not(None),
                    )
                    .order_by(CrawlRun.started_at.desc())
                    .limit(3)
                )
                .scalars()
                .all()
            )
            if len(recent_runs) == 3 and all(r.was_rate_limited for r in recent_runs):
                base_delay = int(
                    float(
                        crawler_payload.get(
                            "crawler_download_delay",
                            DEFAULT_CRAWLER_DOWNLOAD_DELAY,
                        )
                    )
                )
                doubled = min(base_delay * 2, 30)
                crawler_payload["crawler_download_delay"] = doubled
                print(
                    f"Source {source_id}: 3 consecutive rate-limited runs, "
                    f"doubling download_delay to {doubled}s"
                )
    except Exception as exc:
        if crawl_run_id is not None:
            _mark_crawl_run_finished(
                source_id,
                crawl_run_id,
                exit_reason="error",
                notes=f"crawl bootstrap failed: {exc}",
            )
        raise
    finally:
        engine.dispose()

    print(f"Using Scrapy crawler for source [{source_id}]: '{source_title}' ({url})")
    config_json = json.dumps(crawler_payload)
    runner_cmd = [
        sys.executable,
        "-m",
        "jobs.crawler.crawler_runner",
        url,
        str(source_id),
        config_json,
    ]

    try:
        result = subprocess.run(
            runner_cmd,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:
        _mark_crawl_run_finished(
            source_id,
            crawl_run_id,
            exit_reason="error",
            notes=f"crawler_runner launch failed: {exc}",
        )
        raise

    if result.returncode != 0:
        print(f"Crawler failed with exit code {result.returncode}")
        _mark_crawl_run_finished(
            source_id,
            crawl_run_id,
            exit_reason="error",
            notes=f"crawler_runner exit code {result.returncode}",
        )
    else:
        engine = create_sync_engine()
        try:
            with Session(bind=engine) as session:
                source = session.get(Source, source_id)
                if source is None:
                    raise RuntimeError(f"Source {source_id} disappeared after crawl")
                source.last_reindexed_at = datetime.now(timezone.utc)
                session.commit()

                last_run = session.execute(
                    select(CrawlRun)
                    .where(CrawlRun.source_id == source_id)
                    .order_by(CrawlRun.started_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if last_run:
                    duration = None
                    if last_run.started_at and last_run.finished_at:
                        duration = (
                            last_run.finished_at - last_run.started_at
                        ).total_seconds()
                    record_crawl_run(
                        source_id=source_id,
                        pages_new=last_run.pages_new or 0,
                        pages_changed=last_run.pages_changed or 0,
                        pages_crawled=last_run.pages_crawled or 0,
                        pages_errors=last_run.pages_errors or 0,
                        pages_excluded=last_run.pages_excluded or 0,
                        duration_seconds=duration,
                        was_rate_limited=bool(last_run.was_rate_limited),
                    )
        finally:
            engine.dispose()

        print("Triggering index refresh and boilerplate rebuild")
        schedule_refresh_project_index()
        rebuild_boilerplate_index.delay(source_id)

    print(f"Finished crawling source {source_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_page_task",
    queue="celery",
)
def crawl_page_task(page_id: int):
    print(f"Starting crawl for page {page_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            row = session.execute(
                select(Page, Source)
                .join(Source, Source.id == Page.source_id)
                .where(
                    Page.id == page_id,
                    Page.source_id.is_not(None),
                    Page.uri.is_not(None),
                    Source.is_paused.is_(False),
                    Source.blocked_reason.is_(None),
                )
            ).one_or_none()
            if row is None:
                raise RuntimeError(
                    f"Page {page_id} is not refreshable or its source is unavailable"
                )
            page, source = row

            url = page.uri
            source_id = source.id
            crawler_payload = source.config.to_dict()
            crawler_payload["single_page_only"] = True
            tracked_sources: list[dict] = []
            seen_hosts: set[str] = set()
            source_rows = [source]
            source_rows.extend(
                session.execute(select(Source).where(Source.is_paused.is_(False)))
                .scalars()
                .all()
            )
            for tracked_source in source_rows:
                host = (urlparse(tracked_source.uri).hostname or "").lower()
                if not host or host in seen_hosts:
                    continue
                tracked_sources.append(
                    {
                        "id": tracked_source.id,
                        "uri": tracked_source.uri,
                        "rules": list(
                            tracked_source.config.to_dict().get("rules", []) or []
                        ),
                    }
                )
                seen_hosts.add(host)
            crawler_payload["tracked_sources"] = tracked_sources

            recent_runs = (
                session.execute(
                    select(CrawlRun)
                    .where(
                        CrawlRun.source_id == source_id,
                        CrawlRun.finished_at.is_not(None),
                    )
                    .order_by(CrawlRun.started_at.desc())
                    .limit(3)
                )
                .scalars()
                .all()
            )
            if len(recent_runs) == 3 and all(r.was_rate_limited for r in recent_runs):
                base_delay = int(
                    float(
                        crawler_payload.get(
                            "crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY
                        )
                    )
                )
                doubled = min(base_delay * 2, 30)
                crawler_payload["crawler_download_delay"] = doubled
                print(
                    f"Source {source_id}: 3 consecutive rate-limited runs, "
                    f"doubling download_delay to {doubled}s"
                )
    finally:
        engine.dispose()

    print(f"Using Scrapy crawler for page [{page_id}]: {url}")
    config_json = json.dumps(crawler_payload)
    runner_cmd = [
        sys.executable,
        "-m",
        "jobs.crawler.crawler_runner",
        url,
        str(source_id),
        config_json,
    ]

    result = subprocess.run(
        runner_cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        print(f"Page crawler failed with exit code {result.returncode}")
    else:
        should_refresh_index = True
        engine = create_sync_engine()
        try:
            with Session(bind=engine) as session:
                source = session.get(Source, source_id)
                if source is None:
                    raise RuntimeError(
                        f"Source {source_id} disappeared after page crawl"
                    )
                page = session.get(Page, page_id)
                if (
                    page is not None
                    and page.status_error in EXCLUDED_INDEX_STATUS_ERRORS
                ):
                    should_refresh_index = False
                source.last_reindexed_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            engine.dispose()

        if should_refresh_index:
            print("Triggering index refresh and boilerplate rebuild")
            schedule_refresh_project_index()
            rebuild_boilerplate_index.delay(source_id)

    print(f"Finished crawling page {page_id}")


@app.task(name="jobs.crawler.tasks.index_document", queue="celery")
def index_document(document_id: int):
    engine = create_sync_engine()
    redis_client = redis.from_url(REDIS_URL)
    try:
        with Session(bind=engine) as session:
            try:
                index_page_inner(session, document_id)
            except EmbedderDocumentError as exc:
                logging.exception(
                    "Crawler indexing rejected page %s during chunk materialization",
                    document_id,
                )
                mark_page_embedder_failed(
                    session,
                    document_id,
                    exc,
                )
            except Exception as exc:
                logging.exception(
                    "Unexpected crawler indexing failure for page %s",
                    document_id,
                )
                mark_page_embedder_failed(
                    session,
                    document_id,
                    exc,
                    message="Unexpected failure during document chunk materialization.",
                )
    finally:
        try:
            redis_client.delete(index_document_schedule_key(document_id))
        finally:
            redis_client.close()
            engine.dispose()


@app.task(name="jobs.crawler.tasks.ensure_pending_chunks", queue="celery")
def ensure_pending_chunks():
    engine = create_sync_engine()
    redis_client = redis.from_url(REDIS_URL)
    try:
        with Session(bind=engine) as session:
            pending_chunk_count, scheduled = ensure_pending_chunk_workers(
                session,
                redis_client,
                schedule_pending_chunk_tasks,
            )
    finally:
        try:
            redis_client.delete(ENSURE_PENDING_CHUNKS_SCHEDULE_KEY)
        finally:
            redis_client.close()
            engine.dispose()

    logging.info(
        "Ensured pending chunk workers for %s pending chunks; scheduled %s tasks",
        pending_chunk_count,
        scheduled,
    )
    return scheduled


@app.task(name="jobs.crawler.tasks.schedule_pending_chunks", queue="celery")
def schedule_pending_chunks():
    scheduled = schedule_ensure_pending_chunks()
    logging.info("Schedule pending chunks requested; enqueued=%s", scheduled)
    return scheduled


@app.task(name="jobs.crawler.tasks.index_project", queue="celery")
def index_project():
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = (
                select(Page.id)
                .where(Page.status_error.is_(None))
                .where(Page.content.isnot(None))
                .where(Page.content != "")
            )
            doc_ids = session.execute(stmt).scalars().all()
    finally:
        engine.dispose()

    logging.info("Scheduling indexing for %s pages", len(doc_ids))
    for doc_id in doc_ids:
        schedule_index_document(doc_id)


@app.task(name="jobs.crawler.tasks.refresh_project_index", queue="celery")
def refresh_project_index():
    engine = create_sync_engine()
    redis_client = redis.from_url(REDIS_URL)
    try:
        with Session(bind=engine) as session:
            chunk_counts = (
                sa.select(
                    Chunk.page_id,
                    sa.func.count(Chunk.id).label("chunk_count"),
                )
                .join(Page, Chunk.page_id == Page.id)
                .group_by(Chunk.page_id)
                .subquery()
            )

            docs_without_chunks = (
                session.execute(
                    sa.select(Page.id)
                    .outerjoin(chunk_counts, chunk_counts.c.page_id == Page.id)
                    .where(Page.status_error.is_(None))
                    .where(Page.content.isnot(None))
                    .where(Page.content != "")
                    .where(sa.func.coalesce(chunk_counts.c.chunk_count, 0) == 0)
                )
                .scalars()
                .all()
            )

            for doc_id in docs_without_chunks:
                logging.info("Scheduling page %s for refresh indexing", doc_id)
                schedule_index_document(doc_id)

            errored_doc_ids = (
                session.execute(sa.select(Page.id).where(Page.status_error.isnot(None)))
                .scalars()
                .all()
            )

            if errored_doc_ids:
                logging.info(
                    "Removing %s chunk sets for errored pages",
                    len(errored_doc_ids),
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.page_id.in_(errored_doc_ids))
                )

            dangling_chunk_ids = (
                session.execute(
                    sa.select(Chunk.id)
                    .outerjoin(Page, Chunk.page_id == Page.id)
                    .where(Chunk.page_id.isnot(None))
                    .where(Page.id.is_(None))
                )
                .scalars()
                .all()
            )

            if dangling_chunk_ids:
                logging.info(
                    "Cleaning up %s chunk records for deleted pages",
                    len(dangling_chunk_ids),
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.id.in_(dangling_chunk_ids))
                )

            session.commit()
    finally:
        try:
            redis_client.delete(REFRESH_PROJECT_INDEX_SCHEDULE_KEY)
        finally:
            redis_client.close()
            engine.dispose()


@app.task(name="jobs.crawler.tasks.refresh_source_index", queue="celery")
def refresh_source_index(source_id: int):
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if not source:
                logging.warning("Source %s not found", source_id)
                return

            chunk_counts = (
                sa.select(
                    Chunk.page_id,
                    sa.func.count(Chunk.id).label("chunk_count"),
                )
                .join(Page, Chunk.page_id == Page.id)
                .where(Page.source_id == source_id)
                .group_by(Chunk.page_id)
                .subquery()
            )

            docs_without_chunks = (
                session.execute(
                    sa.select(Page.id)
                    .outerjoin(chunk_counts, chunk_counts.c.page_id == Page.id)
                    .where(Page.source_id == source_id)
                    .where(Page.status_error.is_(None))
                    .where(Page.content.isnot(None))
                    .where(Page.content != "")
                    .where(sa.func.coalesce(chunk_counts.c.chunk_count, 0) == 0)
                )
                .scalars()
                .all()
            )

            for doc_id in docs_without_chunks:
                logging.info(
                    "Scheduling page %s for refresh indexing (source %s)",
                    doc_id,
                    source_id,
                )
                schedule_index_document(doc_id)

            errored_doc_ids = (
                session.execute(
                    sa.select(Page.id).where(
                        Page.source_id == source_id,
                        Page.status_error.isnot(None),
                    )
                )
                .scalars()
                .all()
            )

            if errored_doc_ids:
                logging.info(
                    "Removing %s chunk sets for errored pages in source %s",
                    len(errored_doc_ids),
                    source_id,
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.page_id.in_(errored_doc_ids))
                )

            session.commit()
    finally:
        engine.dispose()


@app.task(name="jobs.crawler.tasks.rebuild_boilerplate_index", queue="celery")
def rebuild_boilerplate_index(source_id: int):
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            rebuild_boilerplate_for_source(session, source_id)
    finally:
        engine.dispose()


@app.task(
    name="jobs.crawler.tasks.crawl_all_sources_task",
    queue="celery",
)
def crawl_all_sources_task():
    """
    Crawl all non-upload sources.
    """
    print("Starting crawl for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = select(Source).where(  # noqa: E712
                Source.is_paused == False,
                Source.blocked_reason.is_(None),
            )
            sources = session.execute(stmt).scalars().all()

            source_ids = [source.id for source in sources]
    finally:
        engine.dispose()

    print(f"Found {len(source_ids)} sources to crawl")

    # Trigger crawl_source_task for each source
    for source_id in source_ids:
        crawl_source_task.delay(source_id)

    print("Finished queueing crawl tasks")


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cron_matches_now(cron_expression: str, now: datetime) -> bool:
    parts = cron_expression.split(" ")
    if len(parts) != 5:
        return False

    minute, hour, day_of_month, month_of_year, day_of_week = parts
    schedule = crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        nowfun=lambda: now,
    )
    is_due, next_check_seconds = schedule.is_due(now - timedelta(seconds=60))
    return is_due


def source_is_due_for_reindex(source: Source, now: datetime) -> bool:
    cron_expression = normalize_reindex_cron(
        getattr(source, "reindex_cron", None) or DEFAULT_REINDEX_CRON
    )
    if is_manual_reindex(cron_expression):
        return False

    minute, hour, day_of_month, month_of_year, day_of_week = cron_expression.split(" ")
    schedule = crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        nowfun=lambda: now,
    )
    last_reference = normalize_datetime(getattr(source, "last_reindexed_at", None))
    created_at = normalize_datetime(getattr(source, "created_at", None))
    baseline = last_reference or created_at or (now - timedelta(days=366))
    if baseline > now:
        baseline = now
    is_due, next_check_seconds = schedule.is_due(baseline)
    return is_due


@app.task(
    name="jobs.crawler.tasks.schedule_reindex_sources_task",
    queue="celery",
)
def schedule_reindex_sources_task():
    print("Checking sources for scheduled reindex")

    now = datetime.now(timezone.utc)
    # CrawlRun older than this is considered zombie (worker died without closing the run)
    max_crawl_age = timedelta(days=30)
    queued_ids: list[int] = []

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            print("Updating inlink counts for all pages")
            updated_pages: sa.CursorResult[Any] = session.execute(  # type: ignore
                text(
                    """
                    UPDATE page AS p
                    SET inlink_count = COALESCE(link_counts.cnt, 0)
                    FROM page AS p0
                    LEFT JOIN (
                        SELECT
                            target_page_id,
                            COUNT(*)::integer AS cnt
                        FROM page_link
                        WHERE target_page_id IS NOT NULL
                        GROUP BY target_page_id
                    ) AS link_counts
                        ON link_counts.target_page_id = p0.id
                    WHERE p.id = p0.id
                    """
                )
            )
            print(f"Updated inlink counts for {updated_pages.rowcount or 0} pages")

            print("Running orphan cleanup for all sources")
            deleted_pages: sa.CursorResult[Any] = session.execute(  # type: ignore
                text(
                    """
                    DELETE FROM page AS p
                    WHERE p.http_status IN (404, 410)
                      AND p.error_count >= 2
                      AND p.inlink_count = 0
                      AND p.is_hub_page IS FALSE
                    """
                )
            )
            deleted_count = deleted_pages.rowcount or 0
            if deleted_count == 0:
                print("No orphan pages to delete")
            else:
                print(f"Deleted {deleted_count} orphan pages")
            session.commit()

            sources = (
                session.execute(
                    select(Source).where(  # noqa: E712
                        Source.is_paused == False,
                        Source.blocked_reason.is_(None),
                    )
                )
                .scalars()
                .all()
            )

            for source in sources:
                if not source_is_due_for_reindex(source, now):
                    continue

                # Skip if an active (non-zombie) crawl is already running for this source
                active_run = _find_active_crawl_run(
                    session,
                    source.id,
                    now=now,
                    max_crawl_age=max_crawl_age,
                )
                if active_run:
                    print(
                        f"Source {source.id}: crawl already running "
                        f"(run id={active_run.id}, started {active_run.started_at}), skipping"
                    )
                    continue

                queued_ids.append(source.id)
    finally:
        engine.dispose()

    if not queued_ids:
        print("No sources are due for scheduled reindex")
        return

    print(f"Queueing scheduled reindex for {len(queued_ids)} sources")
    for source_id in queued_ids:
        crawl_source_task.delay(source_id)


@app.task(
    name="jobs.crawler.tasks.reapply_source_rules_task",
    queue="celery",
)
def reapply_source_rules_task(source_id: int) -> int:
    print(f"Reapplying source rules for source {source_id}")

    updated_count = 0
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                raise RuntimeError(f"Source {source_id} not found")

            source_rules = source.config.to_dict().get("rules", [])
            pages = (
                session.execute(select(Page).where(Page.source_id == source_id))
                .scalars()
                .all()
            )

            for page in pages:
                if not page.uri:
                    filtered = False
                else:
                    normalized_url = normalize_url_for_queue(page.uri, source_rules)
                    filtered = bool(
                        normalized_url
                        and (
                            normalized_url != page.uri
                            or not url_allowed_by_rules(normalized_url, source_rules)
                        )
                    )
                if filtered:
                    if page.status_error != PageStatusError.excluded_rules:
                        page.status = PageStatus.crawler
                        page.status_error = PageStatusError.excluded_rules
                        updated_count += 1
                    continue

                if page.status_error == PageStatusError.excluded_rules:
                    page.status_error = None
                    updated_count += 1

            if updated_count:
                session.commit()

        print(
            f"Reapplied source rules for source {source_id}; updated {updated_count} pages"
        )
        return updated_count
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Sitemap sync
# ---------------------------------------------------------------------------

_CRAWLER_USER_AGENT = config.get("crawler_user_agent", "Dzen-AI/1.0")
_SITEMAP_MAX_ENTRIES = 50_000
_SITEMAP_BODY_TOO_LARGE_STATUS = 413
_CRAWLER_DOWNLOAD_MAX_BYTES = int(
    config.get("raw_content_max_bytes", 10 * 1024 * 1024) or 10 * 1024 * 1024
)


def _fetch_sitemap(
    url: str, last_etag: str | None
) -> tuple[int, bytes | None, str | None, str | None]:
    """
    Fetch a sitemap URL with conditional GET.
    Returns (status_code, body_or_None, etag_or_None).
    304 → body is None.
    """
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    if last_etag:
        headers["If-None-Match"] = last_etag

    with requests.get(
        url,
        headers=headers,
        timeout=30,
        allow_redirects=False,
        stream=True,
    ) as resp:
        etag = resp.headers.get("ETag")
        location = resp.headers.get("Location")
        if resp.status_code == 304:
            return 304, None, etag, location
        if resp.status_code == 200:
            content_length = resp.headers.get("Content-Length") or ""
            if (
                content_length.isdigit()
                and int(content_length) > _CRAWLER_DOWNLOAD_MAX_BYTES
            ):
                return _SITEMAP_BODY_TOO_LARGE_STATUS, None, etag, location
            body = resp.raw.read(
                _CRAWLER_DOWNLOAD_MAX_BYTES + 1,
                decode_content=True,
            )
            if len(body) > _CRAWLER_DOWNLOAD_MAX_BYTES:
                return _SITEMAP_BODY_TOO_LARGE_STATUS, None, etag, location
            return 200, body, etag, location
        return resp.status_code, None, etag, location


def _is_valid_sitemap_address(source_uri: str, sitemap_url: str) -> bool:
    parsed = urlparse(sitemap_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    return (parsed.hostname or "").lower() == (
        urlparse(source_uri).hostname or ""
    ).lower()


def _parse_robots_txt(body: str) -> tuple[list[str], int | None]:
    sitemap_urls: list[str] = []
    crawl_delay: int | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        if not _:
            continue
        key_norm = key.strip().lower()
        value_norm = value.strip()
        if not value_norm:
            continue
        if key_norm == "sitemap":
            sitemap_urls.append(value_norm)
        elif key_norm == "crawl-delay":
            try:
                crawl_delay = max(crawl_delay or 0, int(float(value_norm)))
            except ValueError:
                continue
    return sitemap_urls, crawl_delay


def _discover_sitemaps_from_robots(
    source_uri: str,
) -> tuple[list[str], int | None, str | None]:
    robots_url = urljoin(source_uri.rstrip("/") + "/", "robots.txt")
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    with requests.get(
        robots_url,
        headers=headers,
        timeout=15,
        allow_redirects=False,
        stream=True,
    ) as resp:
        if resp.status_code != 200:
            print(f"robots.txt unavailable ({resp.status_code}): {robots_url}")
            return [], None, None

        content_length = resp.headers.get("Content-Length") or ""
        if (
            content_length.isdigit()
            and int(content_length) > _CRAWLER_DOWNLOAD_MAX_BYTES
        ):
            print(f"robots.txt too large: {robots_url}")
            return [], None, None

        raw_body = resp.raw.read(
            _CRAWLER_DOWNLOAD_MAX_BYTES + 1,
            decode_content=True,
        )
        if len(raw_body) > _CRAWLER_DOWNLOAD_MAX_BYTES:
            print(f"robots.txt too large: {robots_url}")
            return [], None, None

        encoding = resp.encoding or "utf-8"
        body = raw_body.decode(encoding, errors="replace")
    sitemap_urls, crawl_delay = _parse_robots_txt(body)
    return sitemap_urls, crawl_delay, body


def _probe_common_sitemaps(source_uri: str) -> list[str]:
    discovered: list[str] = []
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = urljoin(source_uri.rstrip("/") + "/", path.lstrip("/"))
        with requests.get(
            url,
            headers=headers,
            timeout=15,
            allow_redirects=False,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                continue
            content_length = resp.headers.get("Content-Length") or ""
            if (
                content_length.isdigit()
                and int(content_length) > _CRAWLER_DOWNLOAD_MAX_BYTES
            ):
                continue
            body = resp.raw.read(
                _CRAWLER_DOWNLOAD_MAX_BYTES + 1,
                decode_content=True,
            )
            if len(body) > _CRAWLER_DOWNLOAD_MAX_BYTES:
                continue
            content_type = (resp.headers.get("Content-Type") or "").lower()
        if (
            "xml" in content_type
            or b"<urlset" in body[:256]
            or b"<sitemapindex" in body[:256]
        ):
            discovered.append(url)
    return discovered


def _upsert_sitemap(
    session: Session,
    candidate: Sitemap,
) -> None:
    existing = session.execute(
        select(Sitemap).where(
            Sitemap.source_id == candidate.source_id,
            Sitemap.url == candidate.url,
        )
    ).scalar_one_or_none()
    if existing is None:
        candidate.is_excluded = candidate.ignore_reason is not None
        session.add(candidate)
        return

    if candidate.discovered_from_url and not existing.discovered_from_url:
        existing.discovered_from_url = candidate.discovered_from_url
    if candidate.ignore_reason:
        existing.is_excluded = True
        existing.ignore_reason = candidate.ignore_reason


def _refresh_source_discovery(
    session: Session,
    source: Source,
    crawler_payload: dict,
) -> None:
    now = datetime.now(timezone.utc)
    ignore_robots_txt = source.config.ignore_robots_txt
    if ignore_robots_txt:
        source.robots_cache = None
    cached = {} if ignore_robots_txt else source.robots_cache or {}
    cached_at_raw = cached.get("fetched_at")
    cached_at = None
    if isinstance(cached_at_raw, str):
        cached_at = datetime.fromisoformat(cached_at_raw)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)

    sitemap_urls: list[str] = []
    crawl_delay: int | None = None
    robots_body: str | None = None

    if ignore_robots_txt:
        sitemap_urls = []
        crawl_delay = None
    elif cached_at and (now - cached_at) < timedelta(hours=24):
        sitemap_urls = list(cached.get("sitemaps") or [])
        crawl_delay = cached.get("crawl_delay")
    else:
        sitemap_urls, crawl_delay, robots_body = _discover_sitemaps_from_robots(
            source.uri
        )
        source.robots_cache = {
            "fetched_at": now.isoformat(),
            "sitemaps": sitemap_urls,
            "crawl_delay": crawl_delay,
            "body": robots_body,
        }

    if crawl_delay:
        current_delay = int(
            float(
                crawler_payload.get(
                    "crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY
                )
            )
        )
        crawler_payload["crawler_download_delay"] = max(current_delay, crawl_delay)

    discovered_urls = [url for url in sitemap_urls if url]
    if not discovered_urls:
        known_count = session.execute(
            select(func.count(Sitemap.id)).where(Sitemap.source_id == source.id)
        ).scalar_one()
        if known_count == 0:
            discovered_urls.extend(_probe_common_sitemaps(source.uri))

    for sitemap_url in discovered_urls:
        normalized_sitemap_url = normalize_url_for_queue(sitemap_url)
        if not normalized_sitemap_url:
            continue
        if not _is_valid_sitemap_address(source.uri, normalized_sitemap_url):
            _upsert_sitemap(
                session,
                Sitemap(
                    source_id=source.id,
                    url=normalized_sitemap_url,
                    discovered_via="robots_txt"
                    if sitemap_url in sitemap_urls
                    else "auto_probe",
                    ignore_reason="wrong_address",
                ),
            )
            continue
        _upsert_sitemap(
            session,
            Sitemap(
                source_id=source.id,
                url=normalized_sitemap_url,
                discovered_via="robots_txt" if sitemap_url in sitemap_urls else "auto_probe",
            ),
        )

    robots_body_text = None
    if robots_body is not None:
        robots_body_text = robots_body
    else:
        cached_body = (source.robots_cache or {}).get("body")
        if isinstance(cached_body, str) and cached_body.strip():
            robots_body_text = cached_body

    if robots_body_text:
        parser = RobotFileParser()
        parser.set_url(urljoin(source.uri.rstrip("/") + "/", "robots.txt"))
        parser.parse(robots_body_text.splitlines())
        queued_pages = session.execute(
            select(Page.id, Page.uri).where(
                Page.source_id == source.id,
                Page.status == PageStatus.crawler,
                Page.status_error.is_(None),
                Page.uri.is_not(None),
            )
        ).all()
        blocked_page_ids = [
            page_id
            for page_id, page_uri in queued_pages
            if page_uri and not parser.can_fetch(_CRAWLER_USER_AGENT, page_uri)
        ]
        if blocked_page_ids:
            session.execute(
                update(Page)
                .where(Page.id.in_(blocked_page_ids))
                .values(
                    status=PageStatus.ready,
                    status_error=PageStatusError.excluded_robots,
                    last_crawled_at=datetime.now(timezone.utc),
                )
            )


def _sync_sitemaps_for_source(session: Session, source_id: int) -> None:
    source = session.execute(
        select(Source).where(
            Source.id == source_id,
            Source.is_paused.is_(False),
            Source.blocked_reason.is_(None),
        )
    ).scalar_one_or_none()
    if source is None:
        print(f"Source {source_id} is not eligible for sitemap sync, skipping")
        return
    source_rules = [rule.to_dict() for rule in source.config.rules]
    source_rows = session.execute(select(Source.id, Source.uri)).all()
    source_id_by_host = build_source_id_by_host(source_rows)

    sitemaps = (
        session.execute(
            select(Sitemap).where(
                Sitemap.source_id == source_id,
                Sitemap.is_excluded.is_(False),
            )
        )
        .scalars()
        .all()
    )

    if not sitemaps:
        print(f"No active sitemaps for source {source_id}")
        return

    source_page_count = session.execute(
        select(func.count(Page.id)).where(Page.source_id == source_id)
    ).scalar_one()
    force_page_rehydrate = source_page_count == 0
    prioritized_urls: set[str] = set()
    pending_sitemaps = list(sitemaps)
    seen_sitemaps: set[str] = set()

    while pending_sitemaps:
        sm = pending_sitemaps.pop(0)
        if sm.url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm.url)

        if not _is_valid_sitemap_address(source.uri, sm.url):
            sm.is_excluded = True
            sm.ignore_reason = "wrong_address"
            print(f"Sitemap ignored (wrong address): {sm.url}")
            continue

        now = datetime.now(timezone.utc)
        if (
            sm.last_fetched_at is not None
            and sm.last_fetched_at > now - timedelta(days=1)
            and not force_page_rehydrate
        ):
            print(f"Sitemap fetch skipped (<24h since last check): {sm.url}")
            continue

        fetch_etag = None if force_page_rehydrate else sm.last_etag
        status_code, body, etag, location = _fetch_sitemap(sm.url, fetch_etag)

        if status_code == 304:
            sm.last_fetched_at = now
            if etag:
                sm.last_etag = etag
            print(f"Sitemap unchanged (304): {sm.url}")
            continue

        if 300 <= status_code < 400:
            sm.is_excluded = True
            sm.ignore_reason = "redirect"
            print(f"Sitemap ignored (redirect to {location}): {sm.url}")
            continue

        if status_code == _SITEMAP_BODY_TOO_LARGE_STATUS:
            sm.is_excluded = True
            sm.ignore_reason = "too_large"
            print(f"Sitemap ignored (too large): {sm.url}")
            continue

        if status_code != 200 or body is None:
            sm.is_excluded = True
            sm.ignore_reason = f"http_error_{status_code}"
            print(f"Sitemap fetch failed ({status_code}): {sm.url}")
            continue

        if not body.strip():
            sm.is_excluded = True
            sm.ignore_reason = "empty_body"
            print(f"Sitemap ignored (empty body): {sm.url}")
            continue

        sm.ignore_reason = None

        new_hash = hashlib.sha256(body).hexdigest()

        if new_hash == sm.last_content_hash:
            sm.last_fetched_at = now
            if etag:
                sm.last_etag = etag
            print(f"Sitemap hash unchanged, rehydrating pages: {sm.url}")

        try:
            document_kind, parsed_entries = _parse_sitemap_document(body)
        except ValueError as exc:
            sm.is_excluded = True
            sm.ignore_reason = "too_many_entries"
            print(f"Sitemap ignored ({exc}): {sm.url}")
            continue
        if document_kind == "sitemapindex":
            discovered_urls: list[str] = []
            for raw_sitemap_url, _lastmod_str in parsed_entries:
                sitemap_url = normalize_url_for_queue(raw_sitemap_url)
                if not sitemap_url:
                    continue
                if not _is_valid_sitemap_address(source.uri, sitemap_url):
                    _upsert_sitemap(
                        session,
                        Sitemap(
                            source_id=source.id,
                            url=sitemap_url,
                            discovered_via="sitemap_index",
                            discovered_from_url=sm.url,
                            ignore_reason="wrong_address",
                        ),
                    )
                    continue
                _upsert_sitemap(
                    session,
                    Sitemap(
                        source_id=source.id,
                        url=sitemap_url,
                        discovered_via="sitemap_index",
                        discovered_from_url=sm.url,
                    ),
                )
                discovered_urls.append(sitemap_url)
            if discovered_urls:
                session.flush()
                child_sitemaps = (
                    session.execute(
                        select(Sitemap).where(
                            Sitemap.source_id == source_id,
                            Sitemap.url.in_(discovered_urls),
                        )
                    )
                    .scalars()
                    .all()
                )
                pending_sitemaps.extend(child_sitemaps)
        else:
            for raw_page_url, lastmod_str in parsed_entries:
                page_url = normalize_url_for_queue(raw_page_url, source_rules)
                if not page_url or not url_allowed_by_rules(page_url, source_rules):
                    continue
                if not _is_valid_sitemap_address(source.uri, page_url):
                    continue
                page_source_id = resolve_source_id_for_url(
                    page_url,
                    source_id_by_host,
                    fallback_source_id=source.id,
                )
                page = session.execute(
                    select(Page).where(Page.uri == page_url)
                ).scalar_one_or_none()
                if page is None:
                    page = Page(
                        source_id=page_source_id,
                        uri=page_url,
                        discover_by="sitemap",
                        discover_source=sm.url,
                    )
                    page._hash = ""
                    session.add(page)
                    continue

                if lastmod_str is None:
                    continue
                new_lastmod = normalize_datetime(
                    datetime.fromisoformat(lastmod_str.replace("Z", "+00:00"))
                )
                existing_lastmod = normalize_datetime(page.last_modified_at)
                if existing_lastmod is None:
                    prioritized_urls.add(page_url)
                    continue
                if new_lastmod is not None and existing_lastmod < new_lastmod:
                    prioritized_urls.add(page_url)

        sm.last_content_hash = new_hash
        sm.last_fetched_at = now
        sm.url_count = len(parsed_entries)
        if etag:
            sm.last_etag = etag
        print(
            f"Sitemap updated: {sm.url} ({len(parsed_entries)} {document_kind} entries)"
        )

    if prioritized_urls:
        pages_to_prioritize = (
            session.execute(
                select(Page).where(
                    Page.source_id == source_id,
                    Page.uri.in_(list(prioritized_urls)),
                )
            )
            .scalars()
            .all()
        )
        for page in pages_to_prioritize:
            page.check_interval_days = 1
        print(f"Prioritized {len(pages_to_prioritize)} pages with changed lastmod")


def _parse_sitemap_document(body: bytes) -> tuple[str, list[tuple[str, str | None]]]:
    """
    Parse sitemap XML and return:
    - document kind: "urlset" or "sitemapindex"
    - list of (url, lastmod_or_None)
    """
    root = ET.fromstring(body)

    results: list[tuple[str, str | None]] = []
    root_tag = root.tag.rsplit("}", 1)[-1]
    sitemap_ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    tag_url = f"{{{sitemap_ns}}}url"
    tag_loc = f"{{{sitemap_ns}}}loc"
    tag_lastmod = f"{{{sitemap_ns}}}lastmod"
    tag_sitemap = f"{{{sitemap_ns}}}sitemap"
    entry_tags = {tag_url, tag_sitemap}

    for child in root:
        if child.tag in entry_tags:
            loc_el = child.find(tag_loc)
            lastmod_el = child.find(tag_lastmod)
            if loc_el is not None and loc_el.text:
                if len(results) >= _SITEMAP_MAX_ENTRIES:
                    raise ValueError(
                        f"sitemap has more than {_SITEMAP_MAX_ENTRIES} entries"
                    )
                results.append(
                    (
                        loc_el.text.strip(),
                        lastmod_el.text.strip()
                        if lastmod_el is not None and lastmod_el.text
                        else None,
                    )
                )

    if root_tag == "sitemapindex":
        return "sitemapindex", results
    return "urlset", results


@app.task(
    name="jobs.crawler.tasks.sitemap_sync_task",
    queue="celery",
)
def sitemap_sync_task(source_id: int):
    """
    Check all active sitemaps for a source using conditional GET (If-None-Match).
    Pages whose lastmod changed get check_interval_days reset to 1 so they're
    picked up in the next crawl's B-basket.
    """
    print(f"Starting sitemap sync for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            _sync_sitemaps_for_source(session, source_id)
            session.commit()
    finally:
        engine.dispose()

    print(f"Sitemap sync complete for source {source_id}")


@app.task(
    name="jobs.crawler.tasks.schedule_sitemap_sync_task",
    queue="celery",
)
def schedule_sitemap_sync_task():
    """Trigger sitemap_sync_task for all non-paused sources."""
    print("Scheduling sitemap sync for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source_ids = [
                row[0]
                for row in session.execute(
                    select(Source.id).where(
                        Source.is_paused.is_(False),
                        Source.blocked_reason.is_(None),
                    )
                ).all()
            ]
    finally:
        engine.dispose()

    for source_id in source_ids:
        sitemap_sync_task.delay(source_id)


def refresh_source_blocking_state(source_id: int) -> bool:
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                raise RuntimeError(f"Source {source_id} not found")
            result = check_source_blocking(
                source.uri,
                ignore_robots_txt=source.config.ignore_robots_txt,
            )
            apply_source_blocking_result(source, result)
            blocked_reason = result.reason
            if blocked_reason is not None:
                mark_blocked_source_pages_ready(
                    session,
                    source_id=source_id,
                    blocked_reason=blocked_reason.value,
                )
            source.updated_at = datetime.now(timezone.utc)
            session.commit()
            return result.is_blocked
    finally:
        engine.dispose()
