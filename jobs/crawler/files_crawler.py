import logging
import sys
from pathlib import Path

import pypdf
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.models import Chunk, Document, Source
from vchat.models.data import Project

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
    print(f"Starting crawl for file {file_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = (
                select(Document, Source, Project)
                .join(Source, Document.source_id == Source.id)
                .join(Project, Source.project_id == Project.id)
                .where(Document.id == file_id)
            )
            row = session.execute(stmt).first()

            if not row:
                print(f"Document {file_id} not found")
                return

            doc, source, project = row
            file_path_str = doc.uri

            if not file_path_str or not Path(file_path_str).exists():
                print(f"File not found: {file_path_str}")
                _ensure_meta(doc)
                doc.status = "error"
                doc.meta["error"] = "File not found"
                session.commit()
                return

            try:
                text = ""
                ext = Path(file_path_str).suffix.lower()

                if ext == ".pdf":
                    text = extract_pdf(file_path_str)
                elif ext in {".txt", ".md", ".rtf"}:
                    text = extract_text(file_path_str)
                elif ext == ".docx":
                    text = extract_docx(file_path_str)
                else:
                    print(f"Unsupported file type: {ext}")
                    _ensure_meta(doc)
                    doc.status = "error"
                    doc.meta["error"] = f"Unsupported file type: {ext}"
                    session.commit()
                    return

                if not text:
                    print(f"No text extracted from {file_path_str}")
                    doc.status = "indexed"
                    doc.content = ""
                    session.commit()
                    return

                chunk_count = index_document(session, doc, text, project.id)
                print(f"Indexed document {doc.id} with {chunk_count} chunks")

            except Exception as exc:
                print(f"Error processing document {doc.id}: {exc}")
                import traceback

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
                    process_document(session, doc, source.project_id)
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


def process_document(session: Session, doc: Document, project_id: int):
    file_path_str = doc.uri
    if not file_path_str or not Path(file_path_str).exists():
        logger.error(f"File not found: {file_path_str}")
        return

    text = ""
    ext = Path(file_path_str).suffix.lower()

    if ext == ".pdf":
        text = extract_pdf(file_path_str)
    elif ext in {".txt", ".md", ".rtf"}:
        text = extract_text(file_path_str)
    elif ext == ".docx":
        text = extract_docx(file_path_str)
    else:
        logger.warning(f"Unsupported file type: {ext}")
        return

    if not text:
        logger.warning(f"No text extracted from {file_path_str}")
        return

    index_document(session, doc, text, project_id)


def index_document(session: Session, doc: Document, text: str, project_id: int) -> int:
    doc.content = text
    doc.hash_value = text
    doc.length = len(text)
    doc.status = "indexed"

    session.execute(delete(Chunk).where(Chunk.document_id == doc.id))

    chunk_size = 1000
    overlap = 100
    chunks = []
    for i in range(0, len(text), chunk_size - overlap):
        chunk_text = text[i : i + chunk_size]
        if not chunk_text.strip():
            continue

        chunk = Chunk(
            document_id=doc.id,
            chunk_ix=len(chunks),
            content=chunk_text,
            project_id=project_id,
        )
        chunks.append(chunk)

    session.add_all(chunks)
    session.commit()
    return len(chunks)


def extract_pdf(file_path: str) -> str:
    reader = pypdf.PdfReader(file_path)
    text = ""
    for page in reader.pages:
        extracted = page.extract_text() or ""
        text += extracted + "\n"
    return text


def extract_text(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_docx(file_path: str) -> str:
    try:
        import docx

        doc = docx.Document(file_path)
        return "\n".join([para.text for para in doc.paragraphs])
    except ImportError:
        logger.error("python-docx not installed")
        return ""
    except Exception as exc:
        logger.error(f"Error extracting docx: {exc}")
        return ""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m jobs.crawler.files_crawler <source_id>")
        sys.exit(1)

    source_id = int(sys.argv[1])
    crawl_files_source(source_id)
