"""Run against a local pg_search database: VCHAT_EXCEL_DB_URI=postgresql+psycopg://..."""

from __future__ import annotations

import asyncio
import os
from io import BytesIO
from uuid import uuid4

import pytest
import sqlalchemy as sa
from openpyxl import Workbook
from sqlalchemy.orm import Session

from jobs.crawler.document_pipeline import extract_binary_url_document
from jobs.crawler.tasks import materialize_page_chunks
from jobs.embedder import chunking
from tests.test_excel_documents import MIME, WordTokenizer
from vchat.models import Chunk, Page, Source
from vchat.views.chat.ctx import fulltext_supply


@pytest.mark.skipif(
    not os.environ.get("VCHAT_EXCEL_DB_URI"),
    reason="Explicit local PostgreSQL test URI required",
)
def test_excel_cells_materialize_and_are_found_by_real_fulltext_search(monkeypatch):
    monkeypatch.setattr(chunking, "get_embed_tokenizer", lambda: WordTokenizer())
    engine = sa.create_engine(os.environ["VCHAT_EXCEL_DB_URI"])
    keyword = "excelprobe" + uuid4().hex
    with Session(engine) as session:
        before = [
            session.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (Page, Chunk)
        ]
        page_id = None
        source_id = None
        try:
            workbook = Workbook()
            workbook.active.title = "Ресурсы"
            workbook.active.append(["Наименование", "Значение"])
            workbook.active.append([keyword, 125])
            buffer = BytesIO()
            workbook.save(buffer)
            content, title, meta = extract_binary_url_document(
                "https://example.org/local-excel-test.xlsx", buffer.getvalue(), MIME
            )
            source = Source(
                title="Excel integration test", uri=f"https://example.org/{keyword}/"
            )
            session.add(source)
            session.flush()
            source_id = source.id
            page = Page(
                source_id=source_id,
                uri=f"https://example.org/{keyword}.xlsx",
                content=content,
                title=title,
                meta=meta,
            )
            page.hash_value = content
            session.add(page)
            session.flush()
            page_id = page.id
            # pg_search indexes become visible after an actual transaction commit.
            assert materialize_page_chunks(session, page) > 0
            stored = session.scalars(
                sa.select(Chunk).where(Chunk.page_id == page_id)
            ).all()
            assert any(
                keyword in chunk.text and "B2 [Значение]: 125" in chunk.text
                for chunk in stored
            )

            class LocalQuerySession:
                async def execute(self, statement, params):
                    return session.execute(statement, params)

            hits = asyncio.run(
                fulltext_supply(
                    LocalQuerySession(), keyword, allowed_source_ids=[source_id]
                )
            )
            assert any(
                hit.document_id == page_id and keyword in hit.text for hit in hits
            )
            page = session.get(Page, page_id)
            page.content = content.replace("B2 [Значение]: 125", "B2 [Значение]: 126")
            page.hash_value = page.content
            assert materialize_page_chunks(session, page) > 0
            replaced = session.scalars(
                sa.select(Chunk).where(Chunk.page_id == page_id)
            ).all()
            assert any("B2 [Значение]: 126" in chunk.text for chunk in replaced)
            assert all("B2 [Значение]: 125" not in chunk.text for chunk in replaced)
        finally:
            session.rollback()
            if page_id is not None:
                session.execute(sa.delete(Chunk).where(Chunk.page_id == page_id))
                session.execute(sa.delete(Page).where(Page.id == page_id))
                session.commit()
            if source_id is not None:
                session.execute(sa.delete(Source).where(Source.id == source_id))
                session.commit()
        after = [
            session.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (Page, Chunk)
        ]
        assert after == before
    engine.dispose()
