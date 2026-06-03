from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from jobs.db import create_sync_engine


def iter_priority_crawl_queue(
    source_id: int | None,
    *,
    exclude: list[str] | None = None,
    budget: int | None = None,
) -> Iterator[str]:
    """
    Yield URLs to crawl in priority order using the basket algorithm.

    Basket A (20% cap): hub pages (is_hub_page=True) - always crawled for discovery
    Basket B (60% cap): pages with expired check_interval_days
    Basket C (20% cap): error_5xx retry + status='pending'

    If a basket has fewer items than its cap, the remaining budget
    is redistributed to other non-empty baskets in order B→C→A.
    """
    if not source_id:
        return

    excluded = set(exclude or [])
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            unlimited = budget is None
            effective_limit = budget or 1_000_000_000
            cap_a = max(1, int(effective_limit * 0.20))
            cap_b = max(1, int(effective_limit * 0.60))
            cap_c = max(1, int(effective_limit * 0.20))

            # Basket A: hub pages
            basket_a = fetch_basket(
                session,
                source_id,
                excluded,
                extra_filter="is_hub_page = true",
                limit=None if unlimited else effective_limit,
            )

            # Basket B: pages due for recrawl (interval expired) + pending
            basket_b = fetch_basket(
                session,
                source_id,
                excluded,
                extra_filter="""
                    is_hub_page = false
                    AND (status_error IS NULL OR status_error != 'http_5xx')
                    AND (
                        last_crawled_at IS NULL
                        OR (
                            check_interval_days IS NOT NULL AND
                            last_crawled_at + (check_interval_days || ' days')::interval <= NOW()
                        )
                    )
                """,
                limit=None if unlimited else effective_limit,
                order_by="""
                    CASE WHEN last_crawled_at IS NULL THEN 0 ELSE 1 END ASC,
                    COALESCE(
                        EXTRACT(
                            EPOCH FROM (
                                NOW() - (
                                    last_crawled_at + (check_interval_days || ' days')::interval
                                )
                            )
                        ),
                        0
                    ) DESC,
                    COALESCE(last_crawled_at, '1970-01-01'::timestamptz) ASC
                """,
            )

            # Basket C: error_5xx retry
            basket_c = fetch_basket(
                session,
                source_id,
                excluded,
                extra_filter="status_error = 'http_5xx' AND is_hub_page = false",
                limit=None if unlimited else effective_limit,
            )

            alloc_a = min(len(basket_a), cap_a)
            alloc_b = min(len(basket_b), cap_b)
            alloc_c = min(len(basket_c), cap_c)

            remaining = effective_limit - alloc_a - alloc_b - alloc_c

            # Redistribute remaining budget: B → C → A
            if remaining > 0 and len(basket_b) > alloc_b:
                extra = min(remaining, len(basket_b) - alloc_b)
                alloc_b += extra
                remaining -= extra

            if remaining > 0 and len(basket_c) > alloc_c:
                extra = min(remaining, len(basket_c) - alloc_c)
                alloc_c += extra
                remaining -= extra

            if remaining > 0 and len(basket_a) > alloc_a:
                extra = min(remaining, len(basket_a) - alloc_a)
                alloc_a += extra
                remaining -= extra

            queue = basket_a[:alloc_a] + basket_b[:alloc_b] + basket_c[:alloc_c]

            # Deduplicate preserving order
            seen: set[str] = set()
            for url in queue:
                if url not in seen:
                    seen.add(url)
                    yield url
    finally:
        engine.dispose()


def fetch_basket(
    session: Session,
    source_id: int,
    excluded: set[str],
    *,
    extra_filter: str,
    limit: int | None,
    order_by: str = "id ASC",
) -> list[str]:
    limit_sql = "\n            LIMIT :limit" if limit is not None else ""
    params = {"source_id": source_id}
    if limit is not None:
        params["limit"] = limit
    rows = session.execute(
        text(f"""
            SELECT uri FROM page
            WHERE source_id = :source_id
              AND uri IS NOT NULL
              AND ({extra_filter})
            ORDER BY {order_by}
            {limit_sql}
        """),
        params,
    ).all()
    return [r.uri for r in rows if r.uri and r.uri.strip() not in excluded]


# Keep old name as alias for backward compatibility
def iter_source_seed_urls(
    source_id: int | None,
    *,
    exclude=None,
    batch_size: int | None = None,
) -> Iterator[str]:
    """Legacy alias. Use iter_priority_crawl_queue for new code."""
    yield from iter_priority_crawl_queue(
        source_id,
        exclude=list(exclude or []),
        budget=batch_size,
    )
