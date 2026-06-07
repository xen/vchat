#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs.crawler.tasks import crawl_page_task
from jobs.crawler.url_rules import build_source_id_by_host, resolve_source_id_for_url
from jobs.db import create_sync_engine
from vchat.models.data import Chunk, Page, PageLink, Source
from vchat.page_status import PageStatus

DOWNLOADABLE_DOCUMENT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
)


@dataclass(frozen=True)
class RequeuedDocument:
    page_id: int
    source_id: int
    uri: str
    created: bool


def is_downloadable_document_url(uri: str) -> bool:
    return urlparse(uri or "").path.lower().endswith(DOWNLOADABLE_DOCUMENT_EXTENSIONS)


def requeue_downloadable_documents(
    session: Session,
    *,
    source_id: int | None,
    limit: int | None,
) -> list[RequeuedDocument]:
    source_rows = session.execute(
        sa.select(Source.id, Source.uri).where(
            Source.is_paused.is_(False),
            Source.blocked_reason.is_(None),
        )
    ).all()
    source_id_by_host = build_source_id_by_host(source_rows)
    if source_id is not None and source_id not in {row.id for row in source_rows}:
        raise RuntimeError(f"Source {source_id} is not active or does not exist")

    stmt = (
        sa.select(
            PageLink.target_uri,
            PageLink.source_uri,
            PageLink.source_id,
        )
        .where(PageLink.target_uri.isnot(None))
        .distinct()
        .order_by(PageLink.target_uri.asc())
    )
    if source_id is not None:
        stmt = stmt.where(PageLink.source_id == source_id)
    if limit is not None:
        stmt = stmt.limit(limit)

    requeued: list[RequeuedDocument] = []
    seen: set[str] = set()
    for target_uri, source_uri, link_source_id in session.execute(stmt).all():
        if not target_uri or target_uri in seen:
            continue
        seen.add(target_uri)
        if not is_downloadable_document_url(target_uri):
            continue

        page_source_id = resolve_source_id_for_url(
            target_uri,
            source_id_by_host,
            fallback_source_id=int(link_source_id) if link_source_id else None,
        )
        if page_source_id is None:
            raise RuntimeError(f"Cannot resolve source for document URL: {target_uri}")
        if source_id is not None and page_source_id != source_id:
            continue

        page = session.execute(
            sa.select(Page).where(Page.uri == target_uri)
        ).scalar_one_or_none()
        created = page is None
        if page is None:
            page = Page(
                source_id=page_source_id,
                uri=target_uri,
                discover_by="page",
                discover_source=source_uri,
            )
            page.hash_value = ""
            session.add(page)
            session.flush()
        elif page.source_id is None:
            page.source_id = page_source_id
        elif page.source_id != page_source_id:
            raise RuntimeError(
                f"Document {target_uri} belongs to source {page.source_id}, "
                f"but resolved source is {page_source_id}"
            )

        meta = dict(page.meta or {})
        meta["force_reprocess_once"] = True
        page.meta = meta
        page.status = PageStatus.crawler
        page.status_error = None
        page.last_crawled_at = None
        session.execute(sa.delete(Chunk).where(Chunk.page_id == page.id))
        requeued.append(
            RequeuedDocument(
                page_id=page.id,
                source_id=page_source_id,
                uri=target_uri,
                created=created,
            )
        )

    session.commit()
    return requeued


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Requeue downloadable PDF/Word documents discovered in page_link."
    )
    parser.add_argument("--source-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--enqueue", action="store_true")
    args = parser.parse_args()

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            documents = requeue_downloadable_documents(
                session,
                source_id=args.source_id,
                limit=args.limit,
            )
    finally:
        engine.dispose()

    for document in documents:
        state = "created" if document.created else "updated"
        print(
            f"{state} page_id={document.page_id} "
            f"source_id={document.source_id} uri={document.uri}"
        )
        if args.enqueue:
            crawl_page_task.delay(document.page_id)
    print(f"Requeued {len(documents)} downloadable documents")


if __name__ == "__main__":
    main()
