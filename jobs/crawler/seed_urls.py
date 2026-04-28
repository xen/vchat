from __future__ import annotations

from collections.abc import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobs.db import create_sync_engine
from vchat.models.data import Document


def iter_source_seed_urls(
    source_id: int | None,
    *,
    exclude: Iterable[str] | None = None,
    batch_size: int = 5000,
) -> Iterator[str]:
    """Yield stored document URLs for a source in small DB batches.

    This keeps memory usage bounded even for sources with many documents.
    """
    if not source_id:
        return

    excluded = {url for url in (exclude or ()) if url}
    last_id = 0

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            while True:
                rows = session.execute(
                    select(Document.id, Document.uri)
                    .where(
                        Document.source_id == source_id,
                        Document.uri.is_not(None),
                        Document.id > last_id,
                    )
                    .order_by(Document.id)
                    .limit(batch_size)
                ).all()

                if not rows:
                    break

                for document_id, uri in rows:
                    last_id = document_id
                    if not uri:
                        continue
                    normalized = uri.strip()
                    if not normalized or normalized in excluded:
                        continue
                    yield normalized

                if len(rows) < batch_size:
                    break
    finally:
        engine.dispose()
