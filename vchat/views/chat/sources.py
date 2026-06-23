from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from vchat.models import Chunk, Page, Source

from .ctx import normalize_source_summary


SUMMARY_KINDS = ("section_summary", "summary", "file_summary")


def _display_path(
    title: str | None,
    section_path: str | None,
    header_text: str | None,
) -> str | None:
    clean_title = " ".join((title or "").split()).strip()
    branch = " ".join((section_path or header_text or "").split()).strip()
    if clean_title and branch and branch != clean_title:
        return f"{clean_title} / {branch}"
    return clean_title or branch or None


async def enrich_source_payloads(
    db: AsyncSession,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    uri_values = {
        str(source.get("uri") or source.get("page_url") or "").strip()
        for source in sources
        if isinstance(source, dict)
    }
    uris = sorted(uri for uri in uri_values if uri)
    if not uris:
        return sources

    page_result = await db.execute(
        sa.select(
            Page.id,
            Page.uri,
            Page.title,
            Source.title.label("source_title"),
        )
        .outerjoin(Source, Page.source_id == Source.id)
        .where(Page.uri.in_(uris))
    )
    page_rows = list(page_result)
    pages_by_uri = {row.uri: row for row in page_rows}
    page_ids = [row.id for row in page_rows]
    if not page_ids:
        return sources

    summary_result = await db.execute(
        sa.select(Chunk.page_id, Chunk.kind, Chunk.section_path, Chunk.text)
        .where(
            Chunk.page_id.in_(page_ids),
            Chunk.is_duplicate.is_(False),
            Chunk.kind.in_(SUMMARY_KINDS),
        )
        .order_by(
            Chunk.page_id.asc(),
            sa.case(
                (Chunk.kind == "summary", 0),
                (Chunk.kind == "file_summary", 1),
                else_=2,
            ),
            Chunk.id.asc(),
        )
    )
    summary_rows = list(summary_result)

    exact_summary: dict[tuple[int, str], str] = {}
    fallback_summary: dict[int, str] = {}
    for row in summary_rows:
        normalized = normalize_source_summary(row.text)
        if not normalized:
            continue
        if row.section_path:
            exact_summary.setdefault((row.page_id, row.section_path), normalized)
        fallback_summary.setdefault(row.page_id, normalized)

    enriched: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        item = dict(source)
        uri = str(item.get("uri") or item.get("page_url") or "").strip()
        page = pages_by_uri.get(uri)
        if page is not None:
            if not item.get("title") and page.title:
                item["title"] = page.title
            if not item.get("source_title") and page.source_title:
                item["source_title"] = page.source_title
            if not item.get("display_path"):
                item["display_path"] = _display_path(
                    item.get("title"),
                    item.get("section_path"),
                    item.get("header_text"),
                )
            if not item.get("summary"):
                section_path = item.get("section_path")
                summary = None
                if isinstance(section_path, str) and section_path:
                    summary = exact_summary.get((page.id, section_path))
                summary = summary or fallback_summary.get(page.id)
                if summary:
                    item["summary"] = summary
        enriched.append(item)
    return enriched
