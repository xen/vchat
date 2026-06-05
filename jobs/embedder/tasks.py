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
from vchat.embeddings import (
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
from vchat.models import ChatMsg, Chunk, Page
from vchat.page_status import PageStatus
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
            chunk_table.c.token_count,
        )
        .where(chunk_table.c.embedding.is_(None))
        .order_by(chunk_table.c.page_id.asc(), chunk_table.c.chunk_ix.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    chunks = list(session.execute(stmt).all())
    if not chunks:
        return 0

    valid_chunk_ids: list[int] = []
    chunk_rows: list[tuple[int, int, int, str, int]] = []
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
        valid_chunk_ids.append(chunk.id)
        chunk_rows.append(
            (
                chunk.id,
                chunk_page_id,
                chunk.chunk_ix,
                chunk_text,
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

    vectors = make_embed_vectors([row[3] for row in chunk_rows])
    session.execute(
        chunk_table.update()
        .where(chunk_table.c.id == sa.bindparam("chunk_id"))
        .values(embedding=sa.bindparam("embedding_vector")),
        [
            {"chunk_id": chunk_id, "embedding_vector": vec}
            for chunk_id, vec in zip(valid_chunk_ids, vectors, strict=True)
        ],
    )
    session.commit()
    release_torch_cache()

    completed_page_ids: list[int] = []
    if page_ids:
        remaining_rows = session.execute(
            sa.select(chunk_table.c.page_id, sa.func.count(chunk_table.c.id))
            .where(chunk_table.c.page_id.in_(page_ids))
            .where(chunk_table.c.embedding.is_(None))
            .group_by(chunk_table.c.page_id)
        ).all()
        remaining_by_page = {
            int(page_id): int(count or 0) for page_id, count in remaining_rows
        }
        completed_page_ids = [
            page_id for page_id in page_ids if remaining_by_page.get(page_id, 0) == 0
        ]
    if completed_page_ids:
        session.execute(
            Page.__table__.update()
            .where(Page.__table__.c.id.in_(completed_page_ids))
            .values(status=PageStatus.ready, status_error=None)
        )
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
