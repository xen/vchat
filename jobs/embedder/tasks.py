import logging
import time
import gc
from typing import Any, List

import numpy as np
import redis
import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.model import (
    load_embedding_model,
    release_torch_cache,
    resolve_embedding_device,
)
from jobs.embedder.chunking import (
    EMBEDDING_MAX_SEQ_LENGTH,
    EmbedderDocumentError,
    chunk_text_word_window,
)
from jobs.embedder.queue import (
    PENDING_CHUNKS_BATCH_SIZE,
    pending_chunks_remain,
    release_pending_chunk_slots,
)
from jobs.documents.content import (
    CHUNK_TEXT_HASH_IGNORED_CHARS,
    chunk_text_sha256,
    normalize_chunk_text_for_hash,
)
from vchat.models import ChatMsg, Chunk, Page
from vchat.views.projects.page_status import PageStatus
from vchat.settings import config

REDIS_URL = config.get("redis_uri", "redis://localhost:6379/0")
EMBEDDING_ENCODE_BATCH_MAX_CHARS = max(
    1,
    int(
        config.get(
            "embedding_encode_batch_max_chars",
            config.get("embedding_chunk_max_chars", 12000),
        )
        or config.get("embedding_chunk_max_chars", 12000)
    ),
)
EMBEDDING_MODEL_RESET_AFTER_DOCUMENTS = max(
    0, int(config.get("embedding_model_reset_after_documents", 20) or 20)
)

# Lazy, per-process singleton
_embed_model = None
_completed_documents_since_reset = 0


def get_embed_model() -> Any:
    global _embed_model
    if _embed_model is None:
        resolved_device = resolve_embedding_device()
        _embed_model = load_embedding_model(device=resolved_device)
    return _embed_model


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


def embedding_result_to_vectors(embedding_result: Any) -> list[List[float]]:
    vectors = np.asarray(embedding_result)
    if np.isnan(vectors).any():
        raise ValueError("embedding model returned NaN vector")
    return vectors.tolist()


def make_embed_vector(text: str) -> List[float]:
    if not text:
        return []
    emb = get_embed_model().encode(
        [text],
        normalize_embeddings=True,
        batch_size=1,
        show_progress_bar=False,
    )
    return embedding_result_to_vectors(emb)[0]


def make_embed_vectors(texts: list[str]) -> list[List[float]]:
    if not texts:
        return []

    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for text in texts:
        text_len = len(text or "")
        if current and current_chars + text_len > EMBEDDING_ENCODE_BATCH_MAX_CHARS:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(text)
        current_chars += text_len
    if current:
        batches.append(current)

    vectors: list[List[float]] = []
    model = get_embed_model()
    for batch in batches:
        emb = model.encode(
            batch,
            normalize_embeddings=True,
            batch_size=len(batch),
            show_progress_bar=False,
        )
        vectors.extend(embedding_result_to_vectors(emb))
    return vectors


def normalized_chunk_text_expr(column):
    return sa.func.btrim(
        sa.func.translate(
            column,
            CHUNK_TEXT_HASH_IGNORED_CHARS,
            "",
        )
    )


def apply_embedding_to_matching_kb_chunks(
    session: Session,
    *,
    text_hash: str | None,
    text: str,
    embedding: list[float],
) -> set[int]:
    normalized_text = normalize_chunk_text_for_hash(text or "")
    if not normalized_text:
        return set()
    effective_hash = text_hash or chunk_text_sha256(text or "")
    matching_chunk_ids = (
        sa.select(Chunk.id)
        .where(
            Chunk.chat_id.is_(None),
            Chunk.page_id.isnot(None),
            Chunk.embedding.is_(None),
            Chunk.is_duplicate.is_(False),
            Chunk.text_hash == effective_hash,
            normalized_chunk_text_expr(Chunk.text) == normalized_text,
        )
        .with_for_update(skip_locked=True)
        .cte("matching_chunk_ids")
    )
    rows = session.execute(
        sa.update(Chunk)
        .where(Chunk.id.in_(sa.select(matching_chunk_ids.c.id)))
        .values(embedding=embedding)
        .returning(Chunk.page_id)
    ).all()
    return {int(page_id) for (page_id,) in rows if page_id is not None}


def mark_completed_pages(session: Session, page_ids: set[int]) -> list[int]:
    if not page_ids:
        return []
    remaining_rows = session.execute(
        sa.select(Chunk.page_id, sa.func.count(Chunk.id))
        .where(Chunk.page_id.in_(page_ids))
        .where(Chunk.embedding.is_(None))
        .where(Chunk.is_duplicate.is_(False))
        .group_by(Chunk.page_id)
    ).all()
    remaining_by_page = {
        int(page_id): int(count or 0) for page_id, count in remaining_rows
    }
    completed_page_ids = [
        page_id for page_id in page_ids if remaining_by_page.get(page_id, 0) == 0
    ]
    if completed_page_ids:
        session.execute(
            sa.update(Page)
            .where(Page.id.in_(completed_page_ids))
            .values(status=PageStatus.ready, status_error=None)
        )
    return completed_page_ids


def process_next_pending_chunk(session: Session, redis_client: Any = None) -> bool:
    stmt = (
        select(Chunk)
        .where(Chunk.embedding.is_(None), Chunk.is_duplicate.is_(False))
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
    chunk_text_hash = chunk.text_hash
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
    updated_page_ids = apply_embedding_to_matching_kb_chunks(
        session,
        text_hash=chunk_text_hash,
        text=chunk_text,
        embedding=vec,
    )
    if not updated_page_ids:
        chunk.embedding = vec
        updated_page_ids = {chunk_page_id}
    session.flush()
    # Идемпотентность: коммитим каждый чанк сразу
    session.commit()

    # Освобождаем MPS/CUDA кэш после каждого чанка — без этого PyTorch
    # накапливает Metal-буферы до исчерпания памяти при длинных циклах.
    release_torch_cache()

    completed_page_ids = mark_completed_pages(session, updated_page_ids)
    remaining = session.execute(
        sa.select(sa.func.count(Chunk.id)).where(
            Chunk.page_id == chunk_page_id,
            Chunk.embedding.is_(None),
            Chunk.is_duplicate.is_(False),
        )
    ).scalar_one()
    if completed_page_ids:
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

    for _page_id in completed_page_ids:
        maybe_reset_embed_model_after_document()

    return True


def process_pending_chunk_batch(
    session: Session,
    *,
    batch_size: int,
    redis_client: Any = None,
) -> int:
    _ = redis_client
    limit = max(1, int(batch_size or PENDING_CHUNKS_BATCH_SIZE))
    chunk_table = Chunk.__table__
    stmt = (
        select(
            chunk_table.c.id,
            chunk_table.c.page_id,
            chunk_table.c.chunk_ix,
            chunk_table.c.text,
            chunk_table.c.text_hash,
            chunk_table.c.token_count,
        )
        .where(chunk_table.c.embedding.is_(None))
        .where(chunk_table.c.is_duplicate.is_(False))
        .order_by(chunk_table.c.page_id.asc(), chunk_table.c.chunk_ix.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    chunks = list(session.execute(stmt).all())
    if not chunks:
        return 0

    chunk_rows: list[tuple[int, int, int, str, str | None, int]] = []
    page_ids: set[int] = set()
    dangling_chunk_ids: list[int] = []
    total_chars = 0
    for chunk in chunks:
        if chunk.page_id is None:
            logging.warning("Chunk %s has no page reference, deleting", chunk.id)
            dangling_chunk_ids.append(chunk.id)
            continue

        chunk_page_id = int(chunk.page_id)
        if (chunk.token_count or 0) > EMBEDDING_MAX_SEQ_LENGTH:
            raise EmbedderDocumentError(
                f"Chunk {chunk.chunk_ix} for page {chunk_page_id} is too large for embedder "
                f"({chunk.token_count} tokens > {EMBEDDING_MAX_SEQ_LENGTH})",
                page_id=chunk_page_id,
            )
        chunk_text = chunk.text or ""
        chunk_rows.append(
            (
                chunk.id,
                chunk_page_id,
                chunk.chunk_ix,
                chunk_text,
                chunk.text_hash,
                len(chunk_text),
            )
        )
        page_ids.add(chunk_page_id)
        total_chars += len(chunk_text)

    if dangling_chunk_ids:
        session.execute(
            chunk_table.delete().where(chunk_table.c.id.in_(dangling_chunk_ids))
        )

    if dangling_chunk_ids and not chunk_rows:
        session.commit()
        session.expunge_all()
        return len(dangling_chunk_ids)

    start_time = time.monotonic()
    logging.info(
        "Embedding %s chunks across %s pages (%s chars)",
        len(chunk_rows),
        len(page_ids),
        total_chars,
    )

    embedding_inputs: dict[tuple[str, str], str] = {}
    fallback_rows: list[tuple[int, int, int, str, str | None, int]] = []
    for row in chunk_rows:
        _chunk_id, _chunk_page_id, _chunk_ix, chunk_text, text_hash, _text_len = row
        normalized_text = normalize_chunk_text_for_hash(chunk_text)
        if normalized_text and text_hash:
            embedding_inputs.setdefault((text_hash, normalized_text), chunk_text)
        else:
            fallback_rows.append(row)

    embedding_keys = list(embedding_inputs.keys())
    vectors = make_embed_vectors([embedding_inputs[key] for key in embedding_keys])
    updated_page_ids: set[int] = set()
    fallback_updates: list[dict[str, Any]] = []
    for key, vec in zip(embedding_keys, vectors, strict=True):
        text_hash, _normalized_text = key
        chunk_text = embedding_inputs[key]
        matched_page_ids = apply_embedding_to_matching_kb_chunks(
            session,
            text_hash=text_hash,
            text=chunk_text,
            embedding=vec,
        )
        updated_page_ids.update(matched_page_ids)
    if fallback_rows:
        fallback_vectors = make_embed_vectors([row[3] for row in fallback_rows])
        for row, vec in zip(fallback_rows, fallback_vectors, strict=True):
            (
                chunk_id,
                chunk_page_id,
                _chunk_ix,
                _chunk_text,
                _text_hash,
                _text_len,
            ) = row
            fallback_updates.append({"chunk_id": chunk_id, "embedding_vector": vec})
            updated_page_ids.add(chunk_page_id)
    if fallback_updates:
        session.execute(
            chunk_table.update()
            .where(chunk_table.c.id == sa.bindparam("chunk_id"))
            .values(embedding=sa.bindparam("embedding_vector")),
            fallback_updates,
        )
    session.commit()
    release_torch_cache()

    completed_page_ids: list[int] = []
    if updated_page_ids or page_ids:
        completed_page_ids = mark_completed_pages(session, updated_page_ids | page_ids)
    if completed_page_ids:
        session.commit()
        for _page_id in completed_page_ids:
            maybe_reset_embed_model_after_document()

    session.expunge_all()
    duration = time.monotonic() - start_time
    logging.info(
        "Embedded %s chunks across %s pages in %.2fs; deleted %s dangling chunks",
        len(chunk_rows),
        len(page_ids),
        duration,
        len(dangling_chunk_ids),
    )
    return len(chunk_rows) + len(dangling_chunk_ids)


def run_pending_chunk_batch(
    session: Session,
    redis_client: Any,
    *,
    batch_size: int | None = None,
) -> tuple[int, int]:
    limit = max(1, int(batch_size or PENDING_CHUNKS_BATCH_SIZE))
    processed = process_pending_chunk_batch(
        session,
        batch_size=limit,
        redis_client=redis_client,
    )

    remaining = 1 if pending_chunks_remain(session) else 0
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
