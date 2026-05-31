import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import defusedxml.ElementTree as ET
import requests

from celery import chain
from celery.schedules import crontab
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.tasks import (
    rebuild_boilerplate_index,
    schedule_refresh_project_index,
)
from vchat.models.data import CrawlRun, Page, PageLink, Sitemap, Source
from vchat.metrics import record_crawl_run
from vchat.settings import config
from vchat.source_settings import (
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_REINDEX_CRON,
    is_manual_reindex,
    normalize_reindex_cron,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@app.task(
    name="jobs.crawler.tasks.crawl_source_task",
    queue="crawler",
)
def crawl_source_task(source_id: int):
    print(f"Starting crawl for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if not source:
                print(f"Source {source_id} not found")
                return

            if source.is_paused:
                print(f"Source {source_id} is paused, skipping")
                return

            url = source.uri
            source_title = source.title
            crawler_payload = source.config.to_dict()
            crawler_payload["start_pages"] = list(source.start_pages or [])

            recent_runs = (
                session.execute(
                    select(CrawlRun)
                    .where(
                        CrawlRun.source_id == source_id,
                        CrawlRun.finished_at.is_not(None),
                    )
                    .order_by(CrawlRun.started_at.desc())
                    .limit(3)
                )
                .scalars()
                .all()
            )
            if len(recent_runs) == 3 and all(r.was_rate_limited for r in recent_runs):
                base_delay = int(
                    float(
                        crawler_payload.get(
                            "crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY
                        )
                    )
                )
                doubled = min(base_delay * 2, 30)
                crawler_payload["crawler_download_delay"] = doubled
                print(
                    f"Source {source_id}: 3 consecutive rate-limited runs, "
                    f"doubling download_delay to {doubled}s"
                )

            # Mark any existing incomplete CrawlRun for this source as interrupted
            session.execute(
                update(CrawlRun)
                .where(
                    CrawlRun.source_id == source_id,
                    CrawlRun.finished_at.is_(None),
                )
                .values(
                    finished_at=datetime.now(timezone.utc),
                    exit_reason="interrupted",
                )
            )
            session.commit()
    finally:
        engine.dispose()

    print(f"Using Scrapy crawler for source [{source_id}]: '{source_title}' ({url})")
    config_json = json.dumps(crawler_payload)
    runner_cmd = [
        sys.executable,
        "-m",
        "jobs.crawler.crawler_runner",
        url,
        str(source_id),
        config_json,
    ]

    result = subprocess.run(
        runner_cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        print(f"Crawler failed with exit code {result.returncode}")
    else:
        engine = create_sync_engine()
        try:
            with Session(bind=engine) as session:
                source = session.get(Source, source_id)
                if source:
                    source.last_reindexed_at = datetime.now(timezone.utc)
                    session.commit()

                last_run = session.execute(
                    select(CrawlRun)
                    .where(CrawlRun.source_id == source_id)
                    .order_by(CrawlRun.started_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if last_run:
                    duration = None
                    if last_run.started_at and last_run.finished_at:
                        duration = (
                            last_run.finished_at - last_run.started_at
                        ).total_seconds()
                    record_crawl_run(
                        source_id=source_id,
                        pages_new=last_run.pages_new or 0,
                        pages_changed=last_run.pages_changed or 0,
                        pages_crawled=last_run.pages_crawled or 0,
                        pages_errors=last_run.pages_errors or 0,
                        pages_excluded=last_run.pages_excluded or 0,
                        duration_seconds=duration,
                        was_rate_limited=bool(last_run.was_rate_limited),
                    )
        finally:
            engine.dispose()

        print(
            "Triggering inlink update, orphan cleanup, index refresh, and boilerplate rebuild"
        )
        chain(
            update_inlink_counts_task.si(source_id),
            cleanup_orphans_task.si(source_id),
        ).apply_async()
        schedule_refresh_project_index()
        rebuild_boilerplate_index.apply_async(args=[source_id], queue="embeddings")

    print(f"Finished crawling source {source_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_page_task",
    queue="crawler",
)
def crawl_page_task(page_id: int):
    print(f"Starting crawl for page {page_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            page = session.get(Page, page_id)
            if not page:
                print(f"Page {page_id} not found")
                return
            if not page.source_id:
                print(f"Page {page_id} is not attached to a crawl source")
                return
            if not page.uri:
                print(f"Page {page_id} has no URI")
                return

            source = session.get(Source, page.source_id)
            if not source:
                print(f"Source {page.source_id} not found for page {page_id}")
                return

            url = page.uri
            source_id = source.id
            crawler_payload = source.config.to_dict()
            crawler_payload["single_page_only"] = True
            crawler_payload["crawler_max_pages"] = 1

            recent_runs = (
                session.execute(
                    select(CrawlRun)
                    .where(
                        CrawlRun.source_id == source_id,
                        CrawlRun.finished_at.is_not(None),
                    )
                    .order_by(CrawlRun.started_at.desc())
                    .limit(3)
                )
                .scalars()
                .all()
            )
            if len(recent_runs) == 3 and all(r.was_rate_limited for r in recent_runs):
                base_delay = int(
                    float(
                        crawler_payload.get(
                            "crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY
                        )
                    )
                )
                doubled = min(base_delay * 2, 30)
                crawler_payload["crawler_download_delay"] = doubled
                print(
                    f"Source {source_id}: 3 consecutive rate-limited runs, "
                    f"doubling download_delay to {doubled}s"
                )

            session.execute(
                update(CrawlRun)
                .where(
                    CrawlRun.source_id == source_id,
                    CrawlRun.finished_at.is_(None),
                )
                .values(
                    finished_at=datetime.now(timezone.utc),
                    exit_reason="interrupted",
                )
            )
            session.commit()
    finally:
        engine.dispose()

    print(f"Using Scrapy crawler for page [{page_id}]: {url}")
    config_json = json.dumps(crawler_payload)
    runner_cmd = [
        sys.executable,
        "-m",
        "jobs.crawler.crawler_runner",
        url,
        str(source_id),
        config_json,
    ]

    result = subprocess.run(
        runner_cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        print(f"Page crawler failed with exit code {result.returncode}")
    else:
        engine = create_sync_engine()
        try:
            with Session(bind=engine) as session:
                source = session.get(Source, source_id)
                if source:
                    source.last_reindexed_at = datetime.now(timezone.utc)
                    session.commit()
        finally:
            engine.dispose()

        print(
            "Triggering inlink update, orphan cleanup, index refresh, and boilerplate rebuild"
        )
        chain(
            update_inlink_counts_task.si(source_id),
            cleanup_orphans_task.si(source_id),
        ).apply_async()
        schedule_refresh_project_index()
        rebuild_boilerplate_index.apply_async(args=[source_id], queue="embeddings")

    print(f"Finished crawling page {page_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_all_sources_task",
    queue="crawler",
)
def crawl_all_sources_task():
    """
    Crawl all non-upload sources.
    """
    print("Starting crawl for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = select(Source).where(Source.is_paused == False)  # noqa: E712
            sources = session.execute(stmt).scalars().all()

            if not sources:
                print("No sources found")
                return

            source_ids = [source.id for source in sources]
    finally:
        engine.dispose()

    print(f"Found {len(source_ids)} sources to crawl")

    # Trigger crawl_source_task for each source
    for source_id in source_ids:
        crawl_source_task.delay(source_id)

    print("Finished queueing crawl tasks")


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cron_matches_now(cron_expression: str, now: datetime) -> bool:
    parts = cron_expression.split(" ")
    if len(parts) != 5:
        return False

    minute, hour, day_of_month, month_of_year, day_of_week = parts
    schedule = crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        nowfun=lambda: now,
    )
    return schedule.is_due(now - timedelta(seconds=60)).is_due


@app.task(
    name="jobs.crawler.tasks.schedule_reindex_sources_task",
    queue="crawler",
)
def schedule_reindex_sources_task():
    print("Checking sources for scheduled reindex")

    now = datetime.now(timezone.utc)
    queued_ids: list[int] = []

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            sources = (
                session.execute(
                    select(Source).where(Source.is_paused == False)  # noqa: E712
                )
                .scalars()
                .all()
            )

            for source in sources:
                cron_expression = normalize_reindex_cron(
                    getattr(source, "reindex_cron", None) or DEFAULT_REINDEX_CRON
                )

                if is_manual_reindex(cron_expression):
                    continue

                if not cron_matches_now(cron_expression, now):
                    continue

                last_reindex = normalize_datetime(source.last_reindexed_at)
                if last_reindex and (now - last_reindex) < timedelta(days=1):
                    continue

                queued_ids.append(source.id)
    finally:
        engine.dispose()

    if not queued_ids:
        print("No sources are due for scheduled reindex")
        return

    print(f"Queueing scheduled reindex for {len(queued_ids)} sources")
    for source_id in queued_ids:
        crawl_source_task.delay(source_id)


@app.task(
    name="jobs.crawler.tasks.update_inlink_counts_task",
    queue="crawler",
)
def update_inlink_counts_task(source_id: int):
    """Recalculate inlink_count for every page of a source from the PageLink graph."""
    print(f"Updating inlink counts for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            inlink_rows = session.execute(
                select(
                    PageLink.target_page_id,
                    func.count(PageLink.id).label("cnt"),
                )
                .where(PageLink.target_page_id.is_not(None))
                .group_by(PageLink.target_page_id)
            ).all()

            count_map: dict[int, int] = {
                row.target_page_id: row.cnt for row in inlink_rows
            }

            pages = (
                session.execute(select(Page).where(Page.source_id == source_id))
                .scalars()
                .all()
            )

            for page in pages:
                page.inlink_count = count_map.get(page.id, 0)

            session.commit()
            print(f"Updated inlink counts for {len(pages)} pages in source {source_id}")
    finally:
        engine.dispose()


@app.task(
    name="jobs.crawler.tasks.cleanup_orphans_task",
    queue="crawler",
)
def cleanup_orphans_task(source_id: int):
    """Delete dead pages: http_status 404/410, checked ≥2 times in error, no inlinks, not start_pages."""
    print(f"Running orphan cleanup for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if not source:
                print(f"Source {source_id} not found, skipping cleanup")
                return

            start_page_uris: set[str] = set(source.start_pages or [])

            candidates = (
                session.execute(
                    select(Page).where(
                        Page.source_id == source_id,
                        Page.http_status.in_([404, 410]),
                        Page.error_count >= 2,
                        Page.inlink_count == 0,
                        Page.is_hub_page.is_(False),
                    )
                )
                .scalars()
                .all()
            )

            to_delete = [p for p in candidates if p.uri not in start_page_uris]
            if not to_delete:
                print(f"No orphan pages to delete for source {source_id}")
                return

            page_ids = [p.id for p in to_delete]

            # Clear PageLink records referencing these pages before deletion
            session.execute(
                delete(PageLink).where(
                    or_(
                        PageLink.source_page_id.in_(page_ids),
                        PageLink.target_page_id.in_(page_ids),
                    )
                )
            )

            for page in to_delete:
                session.delete(page)

            session.commit()
            print(f"Deleted {len(to_delete)} orphan pages for source {source_id}")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Sitemap sync
# ---------------------------------------------------------------------------

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_CRAWLER_USER_AGENT = config.get("crawler_user_agent", "Dzen-AI/1.0")


def _fetch_sitemap(
    url: str, last_etag: str | None
) -> tuple[int, bytes | None, str | None]:
    """
    Fetch a sitemap URL with conditional GET.
    Returns (status_code, body_or_None, etag_or_None).
    304 → body is None.
    """
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    if last_etag:
        headers["If-None-Match"] = last_etag

    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        print(f"Sitemap fetch error {url}: {exc}")
        return 0, None, None

    etag = resp.headers.get("ETag")
    if resp.status_code == 304:
        return 304, None, etag
    if resp.status_code == 200:
        return 200, resp.content, etag
    return resp.status_code, None, etag


def _parse_sitemap_urls(body: bytes) -> list[tuple[str, str | None]]:
    """
    Parse sitemap XML and return list of (url, lastmod_or_None).
    Handles both <urlset> and <sitemapindex>.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    results: list[tuple[str, str | None]] = []
    tag_url = f"{{{_SITEMAP_NS}}}url"
    tag_loc = f"{{{_SITEMAP_NS}}}loc"
    tag_lastmod = f"{{{_SITEMAP_NS}}}lastmod"
    tag_sitemap = f"{{{_SITEMAP_NS}}}sitemap"

    for child in root:
        if child.tag in (tag_url, tag_sitemap):
            loc_el = child.find(tag_loc)
            lastmod_el = child.find(tag_lastmod)
            if loc_el is not None and loc_el.text:
                results.append(
                    (
                        loc_el.text.strip(),
                        lastmod_el.text.strip()
                        if lastmod_el is not None and lastmod_el.text
                        else None,
                    )
                )

    return results


@app.task(
    name="jobs.crawler.tasks.sitemap_sync_task",
    queue="crawler",
)
def sitemap_sync_task(source_id: int):
    """
    Check all active sitemaps for a source using conditional GET (If-None-Match).
    Pages whose lastmod changed get check_interval_days reset to 1 so they're
    picked up in the next crawl's B-basket.
    """
    print(f"Starting sitemap sync for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            sitemaps = (
                session.execute(
                    select(Sitemap).where(
                        Sitemap.source_id == source_id,
                        Sitemap.is_excluded.is_(False),
                    )
                )
                .scalars()
                .all()
            )

            if not sitemaps:
                print(f"No active sitemaps for source {source_id}")
                return

            prioritized_urls: set[str] = set()

            for sm in sitemaps:
                status_code, body, etag = _fetch_sitemap(sm.url, sm.last_etag)

                if status_code == 304:
                    sm.last_fetched_at = datetime.now(timezone.utc)
                    if etag:
                        sm.last_etag = etag
                    print(f"Sitemap unchanged (304): {sm.url}")
                    continue

                if status_code != 200 or body is None:
                    print(f"Sitemap fetch failed ({status_code}): {sm.url}")
                    continue

                new_hash = hashlib.sha256(body).hexdigest()
                now = datetime.now(timezone.utc)

                if new_hash == sm.last_content_hash:
                    sm.last_fetched_at = now
                    if etag:
                        sm.last_etag = etag
                    print(f"Sitemap hash unchanged: {sm.url}")
                    continue

                parsed_entries = _parse_sitemap_urls(body)

                # Mark pages whose lastmod differs from their last_modified_at
                for page_url, lastmod_str in parsed_entries:
                    if lastmod_str is None:
                        continue
                    try:
                        new_lastmod = datetime.fromisoformat(lastmod_str.rstrip("Z"))
                    except ValueError:
                        continue
                    page = session.execute(
                        select(Page).where(
                            Page.source_id == source_id,
                            Page.uri == page_url,
                        )
                    ).scalar_one_or_none()
                    if page is not None and (
                        page.last_modified_at is None
                        or page.last_modified_at.replace(tzinfo=None) < new_lastmod
                    ):
                        prioritized_urls.add(page_url)

                sm.last_content_hash = new_hash
                sm.last_fetched_at = now
                sm.url_count = len(parsed_entries)
                if etag:
                    sm.last_etag = etag
                print(f"Sitemap updated: {sm.url} ({len(parsed_entries)} URLs)")

            # Reset check_interval_days to 1 for pages with changed lastmod
            if prioritized_urls:
                pages_to_prioritize = (
                    session.execute(
                        select(Page).where(
                            Page.source_id == source_id,
                            Page.uri.in_(list(prioritized_urls)),
                        )
                    )
                    .scalars()
                    .all()
                )
                for page in pages_to_prioritize:
                    page.check_interval_days = 1
                print(
                    f"Prioritized {len(pages_to_prioritize)} pages with changed lastmod"
                )

            session.commit()
    finally:
        engine.dispose()

    print(f"Sitemap sync complete for source {source_id}")


@app.task(
    name="jobs.crawler.tasks.schedule_sitemap_sync_task",
    queue="crawler",
)
def schedule_sitemap_sync_task():
    """Trigger sitemap_sync_task for all non-paused sources."""
    print("Scheduling sitemap sync for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source_ids = [
                row[0]
                for row in session.execute(
                    select(Source.id).where(Source.is_paused.is_(False))
                ).all()
            ]
    finally:
        engine.dispose()

    for source_id in source_ids:
        sitemap_sync_task.delay(source_id)

    print(f"Queued sitemap sync for {len(source_ids)} sources")
