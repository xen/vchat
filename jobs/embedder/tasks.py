import logging
import math
import re
import time
import gc
from dataclasses import dataclass
from typing import Any, List

import redis
import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.embeddings import (
    load_embedding_tokenizer,
    load_embedding_model,
    release_torch_cache,
    resolve_embedding_device,
)
from vchat.document_shingles import is_boilerplate_block
from vchat.models import ChatMsg, Chunk, Page
from vchat.page_status import PageStatus
from vchat.settings import config

REDIS_URL = config.get("redis_uri", "redis://localhost:6379/0")
PENDING_CHUNKS_INFLIGHT_KEY = "vchat:embed:pending_chunks:inflight"
PENDING_CHUNKS_BATCH_SIZE = max(
    1, int(config.get("embedding_pending_chunks_batch_size", 8) or 8)
)
PENDING_CHUNKS_MAX_INFLIGHT = max(
    1, int(config.get("embedding_pending_chunks_max_inflight", 32) or 32)
)
PENDING_CHUNKS_COUNTER_TTL = max(
    60, int(config.get("embedding_pending_chunks_counter_ttl_seconds", 600) or 600)
)
EMBEDDING_MODEL_RESET_AFTER_DOCUMENTS = max(
    0, int(config.get("embedding_model_reset_after_documents", 20) or 20)
)

# Lazy, per-process singleton
_embed_model = None
_embed_tokenizer = None
_completed_documents_since_reset = 0
EMBEDDING_MAX_SEQ_LENGTH = config["embedding_max_seq_length"]
EMBEDDING_CHUNK_MAX_TOKENS = config["embedding_chunk_max_tokens"]
EMBEDDING_CHUNK_OVERLAP_TOKENS = config["embedding_chunk_overlap_tokens"]
EMBEDDING_CHUNK_MAX_CHARS = config["embedding_chunk_max_chars"]
EMBEDDING_BLOCK_MAX_CHARS = max(
    EMBEDDING_CHUNK_MAX_CHARS,
    int(config.get("embedding_block_max_chars", 48000) or 48000),
)
EMBEDDING_ENTITY_SCAN_MAX_CHARS = max(
    EMBEDDING_CHUNK_MAX_CHARS,
    int(config.get("embedding_entity_scan_max_chars", 24000) or 24000),
)


class EmbedderDocumentError(RuntimeError):
    def __init__(self, message: str, *, page_id: int | None = None):
        super().__init__(message)
        self.page_id = page_id


def get_embed_model() -> Any:
    global _embed_model
    if _embed_model is None:
        resolved_device = resolve_embedding_device()
        _embed_model = load_embedding_model(device=resolved_device)
    return _embed_model


def get_embed_tokenizer() -> Any:
    global _embed_tokenizer
    if _embed_tokenizer is None:
        _embed_tokenizer = load_embedding_tokenizer()
    return _embed_tokenizer


def reset_embed_model() -> None:
    global _completed_documents_since_reset, _embed_model
    model = _embed_model
    _embed_model = None
    _completed_documents_since_reset = 0
    if model is not None:
        if hasattr(model, "cpu"):
            model.cpu()
        del model
    gc.collect()
    release_torch_cache()


def maybe_reset_embed_model_after_document() -> bool:
    global _completed_documents_since_reset
    if EMBEDDING_MODEL_RESET_AFTER_DOCUMENTS <= 0:
        return False

    _completed_documents_since_reset += 1
    if _completed_documents_since_reset < EMBEDDING_MODEL_RESET_AFTER_DOCUMENTS:
        return False

    logging.info(
        "Resetting embedding model after %s completed documents",
        _completed_documents_since_reset,
    )
    _completed_documents_since_reset = 0
    reset_embed_model()
    return True


def make_embed_vector(text: str) -> List[float]:
    if not text:
        return []
    emb = get_embed_model().encode([text], normalize_embeddings=True, batch_size=1)
    vec = emb[0].tolist()
    if any(math.isnan(v) for v in vec):
        raise ValueError("embedding model returned NaN vector")
    return vec


@dataclass(frozen=True)
class ChunkData:
    index: int
    start: int | None
    end: int | None
    text: str
    kind: str
    header_text: str | None = None
    section_path: str | None = None
    entity_terms: list[str] | None = None
    token_count: int = 0


def chunk_text_word_window(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap: int | None = None,
) -> List[ChunkData]:
    """
    Split text into overlapping windows by whitespace tokens.
    Returns list of tuples: (chunk_ix, start_offset, end_offset, chunk_text),
    where offsets are token-based indices in the original token list.
    """
    if max_tokens is None:
        max_tokens = EMBEDDING_CHUNK_MAX_TOKENS
    if overlap is None:
        overlap = EMBEDDING_CHUNK_OVERLAP_TOKENS

    tokenizer = get_embed_tokenizer()
    tokens = text.split()
    n = len(tokens)
    chunks: List[ChunkData] = []
    if n == 0:
        return chunks

    i = 0
    ix = 0
    while i < n:
        token = tokens[i]
        if len(token) > EMBEDDING_CHUNK_MAX_CHARS:
            token_ids = tokenizer(token, add_special_tokens=False, truncation=False)[
                "input_ids"
            ]
            for id_start in range(0, len(token_ids), max_tokens):
                piece_ids = token_ids[id_start : id_start + max_tokens]
                piece = tokenizer.decode(
                    piece_ids,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                ).strip()
                if piece:
                    chunks.append(
                        ChunkData(
                            index=ix,
                            start=i,
                            end=i + 1,
                            text=piece,
                            kind="text",
                            token_count=len(piece_ids),
                        )
                    )
                    ix += 1
            i += 1
            continue

        j = i
        char_len = 0
        chunk_tokens: List[str] = []
        while j < n:
            token = tokens[j]
            token_chars = len(token) if not chunk_tokens else len(token) + 1
            candidate_text = " ".join([*chunk_tokens, token])
            candidate_token_len = len(
                tokenizer(
                    candidate_text,
                    add_special_tokens=False,
                    truncation=False,
                )["input_ids"]
            )
            if chunk_tokens and (
                candidate_token_len > max_tokens
                or char_len + token_chars > EMBEDDING_CHUNK_MAX_CHARS
            ):
                break

            if not chunk_tokens and len(token) > EMBEDDING_CHUNK_MAX_CHARS:
                break  # handled at loop start on next iteration

            chunk_tokens.append(token)
            char_len += token_chars
            j += 1
            if candidate_token_len >= max_tokens:
                break

        if not chunk_tokens:
            # token too large, handle next iteration
            i += 1
            continue

        piece = " ".join(chunk_tokens)
        token_len = len(
            tokenizer(
                piece,
                add_special_tokens=False,
                truncation=False,
            )["input_ids"]
        )
        chunks.append(
            ChunkData(
                index=ix,
                start=i,
                end=j,
                text=piece,
                kind="text",
                token_count=token_len,
            )
        )
        ix += 1
        if j >= n:
            break
        # Guarantee forward progress even when overlap is larger than the
        # produced chunk. Without this, short chunks can loop forever.
        i = max(i + 1, j - overlap)
    return chunks


def is_table_separator(line: str) -> bool:
    text = (line or "").strip()
    if "|" not in text:
        return False
    cells = [cell.strip() for cell in text.strip("|").split("|")]
    if not cells:
        return False
    valid = [cell for cell in cells if re.fullmatch(r":?-{3,}:?", cell)]
    return len(valid) >= max(1, len(cells) - 1)


def split_table_rows(table_text: str, max_tokens: int) -> list[str]:
    lines = [line for line in table_text.splitlines() if line.strip()]
    if len(lines) <= 2:
        return [table_text]
    head = lines[:2]
    rows = lines[2:]
    parts: list[str] = []
    bucket: list[str] = []
    tokenizer = get_embed_tokenizer()
    head_tokens = len(
        tokenizer(
            "\n".join(head),
            add_special_tokens=False,
            truncation=False,
        )["input_ids"]
    )

    for row in rows:
        row_tokens = len(
            tokenizer(
                row,
                add_special_tokens=False,
                truncation=False,
            )["input_ids"]
        )
        bucket_tokens = (
            len(
                tokenizer(
                    "\n".join(bucket),
                    add_special_tokens=False,
                    truncation=False,
                )["input_ids"]
            )
            if bucket
            else 0
        )
        if bucket and head_tokens + bucket_tokens + row_tokens > max_tokens:
            parts.append("\n".join(head + bucket))
            bucket = [row]
            continue
        bucket.append(row)

    if bucket:
        parts.append("\n".join(head + bucket))
    return parts or [table_text]


def collect_entity_terms(
    block: str,
    *,
    header_text: str | None = None,
    section_path: str | None = None,
) -> list[str]:
    block = block[:EMBEDDING_ENTITY_SCAN_MAX_CHARS]
    raw_terms: list[str] = []
    if header_text:
        raw_terms.extend(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{3,}", header_text))
    if section_path:
        raw_terms.extend(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{3,}", section_path))
    raw_terms.extend(re.findall(r"\b[A-ZА-Я0-9][A-Za-zА-Яа-я0-9_.-]{2,}\b", block))
    raw_terms.extend(
        [
            term
            for term in re.findall(r"\b[A-Za-zА-Яа-я0-9_.-]{3,}\b", block)
            if any(char.isdigit() for char in term)
            or "." in term
            or "_" in term
            or "-" in term
        ]
    )
    entity_terms: list[str] = []
    seen_terms = set()
    for term in raw_terms:
        normalized = term.strip(".,:;!?()[]{}").lower()
        if len(normalized) < 3 or normalized in seen_terms:
            continue
        seen_terms.add(normalized)
        entity_terms.append(normalized)
        if len(entity_terms) >= 12:
            break
    return entity_terms


def split_text_block_for_chunking(
    text: str,
    *,
    max_chars: int | None = None,
) -> list[str]:
    if max_chars is None:
        max_chars = EMBEDDING_BLOCK_MAX_CHARS

    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    segments: list[str] = []
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()
    ]
    if len(paragraphs) > 1:
        for paragraph in paragraphs:
            segments.extend(
                split_text_block_for_chunking(paragraph, max_chars=max_chars)
            )
        return segments

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+", normalized)
        if part.strip()
    ]
    if len(sentences) > 1:
        bucket: list[str] = []
        bucket_len = 0
        for sentence in sentences:
            addition = len(sentence) if not bucket else len(sentence) + 1
            if bucket and bucket_len + addition > max_chars:
                segments.append(" ".join(bucket))
                bucket = [sentence]
                bucket_len = len(sentence)
                continue
            bucket.append(sentence)
            bucket_len += addition
        if bucket:
            segments.append(" ".join(bucket))
        return segments

    start = 0
    length = len(normalized)
    while start < length:
        end = min(length, start + max_chars)
        if end < length:
            split_at = normalized.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        piece = normalized[start:end].strip()
        if piece:
            segments.append(piece)
        start = end
        while start < length and normalized[start].isspace():
            start += 1
    return segments


def chunk_document_text(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap: int | None = None,
    boilerplate_hashes: frozenset[int] | None = None,
) -> list[ChunkData]:
    if max_tokens is None:
        max_tokens = EMBEDDING_CHUNK_MAX_TOKENS
    if overlap is None:
        overlap = EMBEDDING_CHUNK_OVERLAP_TOKENS

    chunks: list[ChunkData] = []
    lines = text.splitlines()
    blocks: list[tuple[str, str, str | None, str | None]] = []
    section_stack: list[str] = []
    text_lines: list[str] = []
    section_path: str | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            block = "\n".join(text_lines).strip()
            if block:
                blocks.append(
                    (
                        "text",
                        block,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
                text_lines = []
            level = len(heading.group(1))
            title = heading.group(2).strip()
            section_stack = section_stack[: level - 1]
            section_stack.append(title)
            section_path = " / ".join(section_stack)
            index += 1
            continue

        if (
            index + 1 < len(lines)
            and "|" in line
            and is_table_separator(lines[index + 1])
        ):
            block = "\n".join(text_lines).strip()
            if block:
                blocks.append(
                    (
                        "text",
                        block,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
                text_lines = []
            start = index
            index += 2
            while index < len(lines) and "|" in lines[index]:
                index += 1
            table_text = "\n".join(lines[start:index]).strip()
            if table_text:
                blocks.append(
                    (
                        "table",
                        table_text,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
            continue

        text_lines.append(line)
        index += 1

    block = "\n".join(text_lines).strip()
    if block:
        blocks.append(
            (
                "text",
                block,
                section_stack[-1] if section_stack else None,
                section_path,
            )
        )

    if boilerplate_hashes:
        blocks = [
            (kind, block, header_text, section_path)
            for kind, block, header_text, section_path in blocks
            if not is_boilerplate_block(block, boilerplate_hashes)
        ]

    chunk_ix = 0
    tokenizer = get_embed_tokenizer()
    for kind, block, header_text, section_path in blocks:
        entity_terms = collect_entity_terms(
            block,
            header_text=header_text,
            section_path=section_path,
        )

        if kind == "table":
            table_lines = [line for line in block.splitlines() if line.strip()]
            column_names = (
                [
                    cell.strip()
                    for cell in table_lines[0].strip("|").split("|")
                    if cell.strip()
                ]
                if table_lines
                else []
            )
            table_header = " | ".join(column_names) or header_text
            projection_lines = []
            if section_path:
                projection_lines.append(f"Section: {section_path}")
            if table_header:
                projection_lines.append(f"Table header: {table_header}")
            if column_names:
                projection_lines.append(
                    "Columns: " + ", ".join(column_names[: min(len(column_names), 12)])
                )
            if len(table_lines) > 2:
                projection_lines.append("Preview rows:")
                for row in table_lines[2 : min(len(table_lines), 5)]:
                    candidate = "\n".join([*projection_lines, row])
                    candidate_ids = tokenizer(
                        candidate, add_special_tokens=False, truncation=False
                    )["input_ids"]
                    if len(candidate_ids) > max_tokens:
                        break
                    projection_lines.append(row)
            projection_text = "\n".join(projection_lines).strip()
            projection_terms = entity_terms[:]
            for column_name in column_names:
                normalized = column_name.strip().lower()
                if normalized and normalized not in projection_terms:
                    projection_terms.append(normalized)
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=projection_text,
                    kind="table",
                    header_text=table_header,
                    section_path=section_path,
                    entity_terms=projection_terms,
                    token_count=len(
                        tokenizer(
                            projection_text,
                            add_special_tokens=False,
                            truncation=False,
                        )["input_ids"]
                    ),
                )
            )
            chunk_ix += 1
            for part in split_table_rows(block, max_tokens=max_tokens):
                chunks.append(
                    ChunkData(
                        index=chunk_ix,
                        start=None,
                        end=None,
                        text=part,
                        kind="table_rows",
                        header_text=table_header,
                        section_path=section_path,
                        entity_terms=projection_terms,
                        token_count=len(
                            tokenizer(
                                part,
                                add_special_tokens=False,
                                truncation=False,
                            )["input_ids"]
                        ),
                    )
                )
                chunk_ix += 1
            continue

        if len(block.split()) > 24:
            summary_text = " ".join(
                block.split()[: min(len(block.split()), 120)]
            ).strip()
            summary_text = (
                f"Section: {section_path}\nSummary: {summary_text}"
                if section_path
                else f"Summary: {summary_text}"
            )
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=summary_text,
                    kind="section_summary" if section_path else "summary",
                    header_text=header_text,
                    section_path=section_path,
                    entity_terms=entity_terms,
                    token_count=len(
                        tokenizer(
                            summary_text,
                            add_special_tokens=False,
                            truncation=False,
                        )["input_ids"]
                    ),
                )
            )
            chunk_ix += 1

        for segment in split_text_block_for_chunking(block):
            defs = chunk_text_word_window(
                segment,
                max_tokens=max_tokens,
                overlap=overlap,
            )
            for item in defs:
                chunks.append(
                    ChunkData(
                        index=chunk_ix,
                        start=item.start,
                        end=item.end,
                        text=item.text,
                        kind="text",
                        header_text=header_text,
                        section_path=section_path,
                        entity_terms=entity_terms,
                        token_count=item.token_count,
                    )
                )
                chunk_ix += 1

        if entity_terms:
            projection_lines = []
            if section_path:
                projection_lines.append(f"Section: {section_path}")
            projection_lines.append("Entities: " + ", ".join(entity_terms))
            projection_text = "\n".join(projection_lines)
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=projection_text,
                    kind="entity_projection",
                    header_text=header_text,
                    section_path=section_path,
                    entity_terms=entity_terms,
                    token_count=len(
                        tokenizer(
                            projection_text,
                            add_special_tokens=False,
                            truncation=False,
                        )["input_ids"]
                    ),
                )
            )
            chunk_ix += 1

    return chunks


def validate_chunk_data(chunks: list[ChunkData], *, page_id: int) -> None:
    for chunk in chunks:
        if chunk.token_count > EMBEDDING_MAX_SEQ_LENGTH:
            raise EmbedderDocumentError(
                f"Chunk {chunk.index} for page {page_id} is too large for embedder "
                f"({chunk.token_count} tokens > {EMBEDDING_MAX_SEQ_LENGTH})",
                page_id=page_id,
            )
        if (
            chunk.kind in {"text", "table_rows"}
            and len(chunk.text) > EMBEDDING_BLOCK_MAX_CHARS
        ):
            raise EmbedderDocumentError(
                f"Chunk {chunk.index} for page {page_id} exceeds the block char cap "
                f"({len(chunk.text)} chars > {EMBEDDING_BLOCK_MAX_CHARS})",
                page_id=page_id,
            )


def process_next_pending_chunk(session: Session, redis_client: Any = None) -> bool:
    stmt = (
        select(Chunk)
        .where(Chunk.embedding.is_(None))
        .order_by(Chunk.page_id.asc(), Chunk.chunk_ix.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    chunk = session.execute(stmt).scalar_one_or_none()
    if not chunk:
        return False

    if chunk.page_id is None:
        logging.warning("Chunk %s has no page reference, deleting", chunk.id)
        session.delete(chunk)
        session.commit()
        session.expunge_all()
        return True

    doc = session.get(Page, chunk.page_id)
    if doc is None:
        logging.info(
            "Chunk %s references missing page %s, deleting",
            chunk.id,
            chunk.page_id,
        )
        session.delete(chunk)
        session.commit()
        session.expunge_all()
        return True

    # Снимаем скалярные значения до commit/expunge, чтобы не обращаться к
    # detached-объектам после очистки identity map.
    chunk_id = chunk.id
    chunk_page_id = chunk.page_id
    chunk_ix = chunk.chunk_ix
    chunk_text = chunk.text
    chunk_size = len(chunk_text or "")
    start_time = time.monotonic()
    logging.info(
        "Embedding chunk page_id=%s ix=%s size=%s chars",
        chunk_page_id,
        chunk_ix,
        chunk_size,
    )

    if (chunk.token_count or 0) > EMBEDDING_MAX_SEQ_LENGTH:
        raise EmbedderDocumentError(
            f"Chunk {chunk_ix} for page {chunk_page_id} is too large for embedder "
            f"({chunk.token_count} tokens > {EMBEDDING_MAX_SEQ_LENGTH})",
            page_id=chunk_page_id,
        )

    vec = make_embed_vector(chunk_text)
    chunk.embedding = vec
    session.flush()
    # Идемпотентность: коммитим каждый чанк сразу
    session.commit()

    # Освобождаем MPS/CUDA кэш после каждого чанка — без этого PyTorch
    # накапливает Metal-буферы до исчерпания памяти при длинных циклах.
    release_torch_cache()

    remaining = session.execute(
        sa.select(sa.func.count(Chunk.id)).where(
            Chunk.page_id == chunk_page_id,
            Chunk.embedding.is_(None),
        )
    ).scalar_one()

    if remaining == 0:
        session.execute(
            sa.update(Page)
            .where(Page.id == chunk_page_id)
            .values(status=PageStatus.ready, status_error=None)
        )
        session.commit()

    # Очищаем identity map: без этого Session накапливает все Chunk/Page
    # за всё время задачи, что даёт линейный рост памяти.
    session.expunge_all()

    duration = time.monotonic() - start_time
    logging.info(
        "Embedded chunk %s (page_id=%s ix=%s) in %.2fs; %s remaining",
        chunk_id,
        chunk_page_id,
        chunk_ix,
        duration,
        remaining,
    )

    if remaining == 0:
        maybe_reset_embed_model_after_document()

    return True


def count_pending_chunks(session: Session) -> int:
    return int(
        session.execute(
            sa.select(sa.func.count(Chunk.id)).where(Chunk.embedding.is_(None))
        ).scalar_one()
        or 0
    )


def pending_chunk_task_target(pending_chunk_count: int) -> int:
    if pending_chunk_count <= 0:
        return 0
    return min(
        PENDING_CHUNKS_MAX_INFLIGHT,
        max(1, math.ceil(pending_chunk_count / PENDING_CHUNKS_BATCH_SIZE)),
    )


def reserve_pending_chunk_slots(redis_client: Any, target: int) -> int:
    if target <= 0:
        return 0

    return int(
        redis_client.eval(
            """
            local key = KEYS[1]
            local target = tonumber(ARGV[1]) or 0
            local ttl = tonumber(ARGV[2]) or 600
            local current = tonumber(redis.call('GET', key) or '0')
            if current >= target then
                if current > 0 and ttl > 0 then
                    redis.call('EXPIRE', key, ttl)
                end
                return 0
            end
            local missing = target - current
            redis.call('INCRBY', key, missing)
            if ttl > 0 then
                redis.call('EXPIRE', key, ttl)
            end
            return missing
            """,
            1,
            PENDING_CHUNKS_INFLIGHT_KEY,
            target,
            PENDING_CHUNKS_COUNTER_TTL,
        )
        or 0
    )


def release_pending_chunk_slots(redis_client: Any, slots: int = 1) -> int:
    slots = max(1, int(slots or 1))
    return int(
        redis_client.eval(
            """
            local key = KEYS[1]
            local release = tonumber(ARGV[1]) or 1
            local ttl = tonumber(ARGV[2]) or 600
            local current = tonumber(redis.call('GET', key) or '0')
            if current <= release then
                redis.call('DEL', key)
                return 0
            end
            local next = current - release
            redis.call('SET', key, tostring(next))
            if ttl > 0 then
                redis.call('EXPIRE', key, ttl)
            end
            return next
            """,
            1,
            PENDING_CHUNKS_INFLIGHT_KEY,
            slots,
            PENDING_CHUNKS_COUNTER_TTL,
        )
        or 0
    )


def ensure_pending_chunk_workers(
    session: Session, redis_client: Any, schedule_tasks
) -> tuple[int, int]:
    pending_chunk_count = count_pending_chunks(session)
    target = pending_chunk_task_target(pending_chunk_count)
    if target == 0:
        return pending_chunk_count, 0

    missing = reserve_pending_chunk_slots(redis_client, target)
    if missing <= 0:
        return pending_chunk_count, 0

    scheduled = 0
    try:
        scheduled = schedule_tasks(missing)
        return pending_chunk_count, scheduled
    finally:
        unscheduled = missing - scheduled
        if unscheduled > 0:
            release_pending_chunk_slots(redis_client, unscheduled)


def run_pending_chunk_batch(
    session: Session,
    redis_client: Any,
    *,
    batch_size: int | None = None,
) -> tuple[int, int]:
    processed = 0
    limit = max(1, int(batch_size or PENDING_CHUNKS_BATCH_SIZE))
    while processed < limit and process_next_pending_chunk(
        session, redis_client=redis_client
    ):
        processed += 1

    remaining = count_pending_chunks(session)
    return processed, remaining


@app.task(name="jobs.embedder.tasks.index_chat_message", queue="embeddings")
def index_chat_message(msg_id: int):
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = select(ChatMsg).where(ChatMsg.id == msg_id)
            msg = session.scalar(stmt)

            if not msg:
                logging.warning("ChatMsg %s not found", msg_id)
                return

            text = msg.text or ""
            chunks = chunk_text_word_window(text)

            session.execute(delete(Chunk).where(Chunk.msg_id == msg_id))

            if not chunks:
                logging.info("No content to index for ChatMsg %s", msg_id)
                session.commit()
                return

            for c in chunks:
                vec = make_embed_vector(c.text)
                chunk = Chunk(
                    chat_id=msg.chat_id,
                    user_uid=msg.user_uid,
                    msg_id=msg.id,
                    page_id=None,
                    chunk_ix=c.index,
                    start_offset=c.start,
                    end_offset=c.end,
                    kind="text",
                    header_text=None,
                    section_path=None,
                    entity_terms=None,
                    token_count=c.token_count,
                    text=c.text,
                    embedding=vec,
                )
                session.add(chunk)

            session.commit()
            logging.info("Indexed ChatMsg %s into %d chunks", msg_id, len(chunks))
    finally:
        engine.dispose()


@app.task(name="jobs.embedder.tasks.pending_chunks", queue="embeddings")
def pending_chunks(counted: bool = False):
    engine = create_sync_engine()
    redis_client = redis.from_url(REDIS_URL)
    processed = 0
    remaining = 0
    try:
        with Session(bind=engine) as session:
            try:
                processed, remaining = run_pending_chunk_batch(session, redis_client)
            except EmbedderDocumentError as exc:
                logging.exception("Embedder rejected a pending chunk batch")
                if exc.page_id is not None:
                    from jobs.crawler.tasks import mark_page_embedder_failed

                    mark_page_embedder_failed(
                        session,
                        exc.page_id,
                        message=str(exc),
                        error=str(exc),
                        exception_class=type(exc).__name__,
                    )
                else:
                    raise
    finally:
        try:
            if counted:
                release_pending_chunk_slots(redis_client)
        finally:
            redis_client.close()
            engine.dispose()

    if remaining > 0:
        app.send_task("jobs.crawler.tasks.ensure_pending_chunks", queue="celery")

    logging.info(
        "Processed %s pending chunks in batch; %s remaining",
        processed,
        remaining,
    )
    return processed
