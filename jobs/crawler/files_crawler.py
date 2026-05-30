import logging
import sys
import traceback
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.tasks import schedule_index_document
from vchat.document_pipeline import extract_local_file_document
from vchat.document_indexing import (
    document_content_effectively_unchanged,
    sync_document_has_chunks,
)
from vchat.models import Document, Source

logger = logging.getLogger(__name__)


def _ensure_meta(doc: Document) -> None:
    """Ensure the document meta field is a mutable dict."""
    if doc.meta is None:
        doc.meta = {}
    elif not isinstance(doc.meta, dict):
        doc.meta = dict(doc.meta)


@app.task(
    name="jobs.crawler.files_crawler.crawl_file_task",
    queue="crawler",
)
def crawl_file_task(file_id: int):
    logger.info("Starting crawl for file %s", file_id)

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            doc = session.scalar(select(Document).where(Document.id == file_id))
            if not doc:
                logger.warning("Document %s not found", file_id)
                return
            file_path_str = doc.uri

            if not file_path_str or not Path(file_path_str).exists():
                logger.warning("File not found: %s", file_path_str)
                _ensure_meta(doc)
                doc.status = "error"
                doc.meta["error"] = "File not found"
                session.commit()
                return

            try:
                content, title, meta = extract_local_file_document(file_path_str)

                if not content:
                    logger.warning("No text extracted from %s", file_path_str)
                    _ensure_meta(doc)
                    doc.index_status = "indexed"
                    doc.content = ""
                    doc.length = 0
                    doc.hash_value = ""
                    session.commit()
                    return

                _ensure_meta(doc)
                merged_meta = dict(doc.meta or {})
                merged_meta.update(meta)

                effectively_unchanged = document_content_effectively_unchanged(
                    doc, content
                )
                has_chunks = (
                    sync_document_has_chunks(session, doc.id)
                    if (effectively_unchanged and doc.id is not None)
                    else False
                )
                doc.content = content
                doc.hash_value = content
                doc.length = len(content)
                doc.index_status = "queued"
                doc.language = ""
                doc.meta = merged_meta
                if title:
                    doc.title = title
                session.commit()

                if effectively_unchanged and has_chunks:
                    doc.index_status = "indexed"
                    session.commit()
                    logger.info(
                        "Skipping chunk refresh for file document %s: content unchanged",
                        doc.id,
                    )
                else:
                    schedule_index_document(doc.id)
                    logger.info(
                        "Normalized file document %s and scheduled indexing", doc.id
                    )

            except Exception as exc:
                logger.exception("Error processing document %s", doc.id)
                traceback.print_exc()
                session.rollback()
                _ensure_meta(doc)
                doc.status = "error"
                doc.meta["error"] = str(exc)
                session.commit()

    finally:
        engine.dispose()


def crawl_files_source(source_id: int):
    """
    Process uploaded files for a source.
    """
    logger.info(f"Starting files crawl for source {source_id}")
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if not source:
                logger.error(f"Source {source_id} not found")
                return

            stmt = select(Document).where(Document.source_id == source.id)
            documents = session.execute(stmt).scalars().all()

            for doc in documents:
                try:
                    process_document(session, doc)
                except Exception as exc:
                    logger.error(
                        f"Error processing document {doc.id} ({doc.title}): {exc}"
                    )
                    session.rollback()
                    _ensure_meta(doc)
                    doc.status = "error"
                    doc.meta["error"] = str(exc)
                    session.commit()
    finally:
        engine.dispose()


def process_document(session: Session, doc: Document):
    file_path_str = doc.uri
    if not file_path_str or not Path(file_path_str).exists():
        logger.error(f"File not found: {file_path_str}")
        return

    content, title, meta = extract_local_file_document(file_path_str)
    if not content:
        logger.warning(f"No text extracted from {file_path_str}")
        return

    _ensure_meta(doc)
    merged_meta = dict(doc.meta or {})
    merged_meta.update(meta)
    effectively_unchanged = document_content_effectively_unchanged(doc, content)
    has_chunks = (
        sync_document_has_chunks(session, doc.id)
        if (effectively_unchanged and doc.id is not None)
        else False
    )
    doc.content = content
    doc.hash_value = content
    doc.length = len(content)
    doc.index_status = (
        "queued" if not (effectively_unchanged and has_chunks) else "indexed"
    )
    doc.language = ""
    doc.meta = merged_meta
    if title:
        doc.title = title
    session.commit()
    if not (effectively_unchanged and has_chunks):
        schedule_index_document(doc.id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m jobs.crawler.files_crawler <source_id>")
        sys.exit(1)

    source_id = int(sys.argv[1])
    crawl_files_source(source_id)
