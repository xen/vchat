import logging
import re
import time
from dataclasses import dataclass
from typing import Any, List

import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.embeddings import load_embedding_model
from vchat.models import ChatMsg, Chunk, Document, Source
from vchat.settings import config

# Lazy, per-process singleton
_embed_model = None
EMBEDDING_MAX_SEQ_LENGTH = config["embedding_max_seq_length"]
EMBEDDING_CHUNK_MAX_TOKENS = config["embedding_chunk_max_tokens"]
EMBEDDING_CHUNK_OVERLAP_TOKENS = config["embedding_chunk_overlap_tokens"]
EMBEDDING_CHUNK_MAX_CHARS = config["embedding_chunk_max_chars"]


def get_embed_model() -> Any:
    global _embed_model
    if _embed_model is None:
        logging.info("Loading embedding model.")
        _embed_model = load_embedding_model(device="cpu")
        logging.info("Embedding model loaded.")
    return _embed_model


def make_embed_vector(text: str) -> List[float]:
    if not text:
        return []
    emb = get_embed_model().encode([text], normalize_embeddings=True, batch_size=1)
    return emb[0].tolist()


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
    max_tokens: int = EMBEDDING_CHUNK_MAX_TOKENS,
    overlap: int = EMBEDDING_CHUNK_OVERLAP_TOKENS,
) -> List[ChunkData]:
    """
    Split text into overlapping windows by whitespace tokens.
    Returns list of tuples: (chunk_ix, start_offset, end_offset, chunk_text),
    where offsets are token-based indices in the original token list.
    """
    tokenizer = get_embed_model().tokenizer
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
            for start in range(0, len(token), EMBEDDING_CHUNK_MAX_CHARS):
                piece = token[start : start + EMBEDDING_CHUNK_MAX_CHARS]
                chunks.append(
                    ChunkData(
                        index=ix,
                        start=i,
                        end=i + 1,
                        text=piece,
                        kind="text",
                        token_count=len(
                            tokenizer(
                                piece,
                                add_special_tokens=False,
                                truncation=False,
                            )["input_ids"]
                        ),
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
        i = max(0, j - overlap)
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
    tokenizer = get_embed_model().tokenizer
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
        bucket_tokens = len(
            tokenizer(
                "\n".join(bucket),
                add_special_tokens=False,
                truncation=False,
            )["input_ids"]
        ) if bucket else 0
        if bucket and head_tokens + bucket_tokens + row_tokens > max_tokens:
            parts.append("\n".join(head + bucket))
            bucket = [row]
            continue
        bucket.append(row)

    if bucket:
        parts.append("\n".join(head + bucket))
    return parts or [table_text]


def _collect_entity_terms(
    block: str,
    *,
    header_text: str | None = None,
    section_path: str | None = None,
) -> list[str]:
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


def chunk_document_text(
    text: str,
    *,
    max_tokens: int = EMBEDDING_CHUNK_MAX_TOKENS,
    overlap: int = EMBEDDING_CHUNK_OVERLAP_TOKENS,
) -> list[ChunkData]:
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

        if index + 1 < len(lines) and "|" in line and is_table_separator(lines[index + 1]):
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

    chunk_ix = 0
    tokenizer = get_embed_model().tokenizer
    for kind, block, header_text, section_path in blocks:
        entity_terms = _collect_entity_terms(
            block,
            header_text=header_text,
            section_path=section_path,
        )

        if kind == "table":
            table_lines = [line for line in block.splitlines() if line.strip()]
            column_names = [
                cell.strip()
                for cell in table_lines[0].strip("|").split("|")
                if cell.strip()
            ] if table_lines else []
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
                projection_lines.extend(table_lines[2 : min(len(table_lines), 5)])
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

        block_tokens = tokenizer(
            block,
            add_special_tokens=False,
            truncation=False,
        )["input_ids"]
        if len(block_tokens) > 24:
            summary_text = " ".join(block.split()[: min(len(block.split()), 120)]).strip()
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

        defs = chunk_text_word_window(block, max_tokens=max_tokens, overlap=overlap)
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


def _fetch_document_context(session: Session, document_id: int):
    stmt = select(Document).where(Document.id == document_id)
    row = session.execute(stmt).first()
    if not row:
        logging.warning("Document %s not found", document_id)
        return None

    (doc,) = row

    if not doc.content:
        logging.warning("Document %s has no content", document_id)
        return None

    if doc.is_ignored:
        logging.info("Document %s is ignored, skipping", document_id)
        return None

    return doc


def _materialize_document_chunks(
    session: Session, doc: Document, user_uid: str = "system"
) -> int:
    chunks = chunk_document_text(doc.content or "")
    logging.info("Materializing %s chunks for Document %s", len(chunks), doc.id)

    session.execute(delete(Chunk).where(Chunk.document_id == doc.id))

    if not chunks:
        logging.info("No content to index for Document %s", doc.id)
        doc.status = "indexed"
        session.commit()
        return 0

    for c in chunks:
        chunk = Chunk(
            chat_id=None,
            user_uid=user_uid,
            msg_id=None,
            document_id=doc.id,
            chunk_ix=c.index,
            start_offset=c.start,
            end_offset=c.end,
            kind=c.kind,
            header_text=c.header_text,
            section_path=c.section_path,
            entity_terms=c.entity_terms,
            token_count=c.token_count,
            text=c.text,
            embedding=None,
        )
        session.add(chunk)

    doc.status = "added"
    session.commit()
    return len(chunks)


def _process_next_pending_chunk(session: Session) -> bool:
    stmt = (
        select(Chunk)
        .where(Chunk.embedding.is_(None))
        .order_by(Chunk.document_id.asc(), Chunk.chunk_ix.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    chunk = session.execute(stmt).scalar_one_or_none()
    if not chunk:
        return False

    if chunk.document_id is None:
        logging.warning("Chunk %s has no document reference, deleting", chunk.id)
        session.delete(chunk)
        session.commit()
        return True

    doc = session.get(Document, chunk.document_id)
    if doc is None:
        logging.info(
            "Chunk %s references missing document %s, deleting",
            chunk.id,
            chunk.document_id,
        )
        session.delete(chunk)
        session.commit()
        return True

    chunk_size = len(chunk.text or "")
    start_time = time.monotonic()
    logging.info(
        "Embedding chunk doc_id=%s ix=%s size=%s chars",
        chunk.document_id,
        chunk.chunk_ix,
        chunk_size,
    )

    vec = make_embed_vector(chunk.text)
    chunk.embedding = vec
    session.flush()

    remaining = session.execute(
        sa.select(sa.func.count(Chunk.id)).where(
            Chunk.document_id == chunk.document_id,
            Chunk.embedding.is_(None),
        )
    ).scalar_one()

    if remaining == 0:
        doc.status = "indexed"

    session.commit()
    duration = time.monotonic() - start_time
    logging.info(
        "Embedded chunk %s (doc_id=%s ix=%s) in %.2fs; %s remaining",
        chunk.id,
        chunk.document_id,
        chunk.chunk_ix,
        duration,
        remaining,
    )
    return True


def _index_document_chunks(
    session: Session, doc: Document
) -> bool:
    chunk_count = _materialize_document_chunks(session, doc)
    if chunk_count == 0:
        return False

    pending_chunks.delay()
    return True


def _index_document_inner(session: Session, document_id: int) -> bool:
    context = _fetch_document_context(session, document_id)
    if not context:
        return False

    return _index_document_chunks(session, context)


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
                    document_id=None,
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


@app.task(name="jobs.embedder.tasks.index_document", queue="embeddings")
def index_document(document_id: int):
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            _index_document_inner(session, document_id)
    finally:
        engine.dispose()


@app.task(name="jobs.embedder.tasks.pending_chunks", queue="embeddings")
def pending_chunks():
    engine = create_sync_engine()
    processed = 0
    try:
        with Session(bind=engine) as session:
            while _process_next_pending_chunk(session):
                processed += 1
    finally:
        engine.dispose()

    logging.info("Processed %s pending chunks", processed)
    return processed


@app.task(name="jobs.embedder.tasks.index_project", queue="embeddings")
def index_project():
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = (
                select(Document.id)
                .where(Document.is_ignored == False)
            )
            doc_ids = session.execute(stmt).scalars().all()
    finally:
        engine.dispose()

    logging.info(
        "Scheduling indexing for %s documents",
        len(doc_ids),
    )

    for doc_id in doc_ids:
        index_document.delay(doc_id)


@app.task(name="jobs.embedder.tasks.refresh_project_index", queue="embeddings")
def refresh_project_index():
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            chunk_counts = (
                sa.select(
                    Chunk.document_id,
                    sa.func.count(Chunk.id).label("chunk_count"),
                )
                .join(Document, Chunk.document_id == Document.id)
                .group_by(Chunk.document_id)
                .subquery()
            )

            docs_without_chunks = (
                session.execute(
                    sa.select(Document.id)
                    .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
                    .where(Document.is_ignored == False)
                    .where(sa.func.coalesce(chunk_counts.c.chunk_count, 0) == 0)
                )
                .scalars()
                .all()
            )

            for doc_id in docs_without_chunks:
                logging.info("Scheduling document %s for refresh indexing", doc_id)
                index_document.delay(doc_id)

            ignored_doc_ids = (
                session.execute(
                    sa.select(Document.id)
                    .where(Document.is_ignored == True)
                )
                .scalars()
                .all()
            )

            if ignored_doc_ids:
                logging.info(
                    "Removing %s chunk sets for ignored documents",
                    len(ignored_doc_ids),
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.document_id.in_(ignored_doc_ids))
                )

            dangling_chunk_ids = (
                session.execute(
                    sa.select(Chunk.id)
                    .outerjoin(Document, Chunk.document_id == Document.id)
                    .where(Chunk.document_id.isnot(None))
                    .where(Document.id.is_(None))
                )
                .scalars()
                .all()
            )

            if dangling_chunk_ids:
                logging.info(
                    "Cleaning up %s chunk records for deleted documents",
                    len(dangling_chunk_ids),
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.id.in_(dangling_chunk_ids))
                )

            session.commit()
    finally:
        engine.dispose()


@app.task(name="jobs.embedder.tasks.refresh_source_index", queue="embeddings")
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
                    Chunk.document_id,
                    sa.func.count(Chunk.id).label("chunk_count"),
                )
                .join(Document, Chunk.document_id == Document.id)
                .where(Document.source_id == source_id)
                .group_by(Chunk.document_id)
                .subquery()
            )

            docs_without_chunks = (
                session.execute(
                    sa.select(Document.id)
                    .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
                    .where(Document.source_id == source_id)
                    .where(Document.is_ignored == False)
                    .where(sa.func.coalesce(chunk_counts.c.chunk_count, 0) == 0)
                )
                .scalars()
                .all()
            )

            for doc_id in docs_without_chunks:
                logging.info(
                    "Scheduling document %s for refresh indexing (source %s)",
                    doc_id,
                    source_id,
                )
                index_document.delay(doc_id)

            ignored_doc_ids = (
                session.execute(
                    sa.select(Document.id).where(
                        Document.source_id == source_id, Document.is_ignored == True
                    )
                )
                .scalars()
                .all()
            )

            if ignored_doc_ids:
                logging.info(
                    "Removing %s chunk sets for ignored documents in source %s",
                    len(ignored_doc_ids),
                    source_id,
                )
                session.execute(
                    sa.delete(Chunk).where(Chunk.document_id.in_(ignored_doc_ids))
                )

            session.commit()
    finally:
        engine.dispose()
