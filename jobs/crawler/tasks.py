import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import defusedxml.ElementTree as ET
import requests

from celery import chain
from celery.schedules import crontab
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.tasks import (
    rebuild_boilerplate_index,
    schedule_refresh_project_index,
)
from jobs.crawler.source_routing import (
    build_source_id_by_host,
    resolve_source_id_for_url,
)
from jobs.crawler.url_rules import normalize_url_for_queue, url_allowed_by_rules
from vchat.models.data import CrawlRun, Page, Sitemap, Source
from vchat.metrics import record_crawl_run
from vchat.page_status import PageStatus, PageStatusError
from vchat.settings import config
from vchat.source_blocking import apply_source_blocking_result, check_source_blocking
from vchat.source_settings import (
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_REINDEX_CRON,
    is_manual_reindex,
    normalize_reindex_cron,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ROBOTS_CACHE_TTL = timedelta(hours=24)
_SITEMAP_FETCH_INTERVAL = timedelta(days=1)
_AUTO_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
_CRAWL_LOCK_NAMESPACE = 90421
SITEMAP_IGNORE_WRONG_ADDRESS = "wrong_address"
SITEMAP_IGNORE_REDIRECT = "redirect"
SITEMAP_IGNORE_EMPTY_BODY = "empty_body"
SITEMAP_IGNORE_HTTP_ERROR_PREFIX = "http_error_"


def _source_rules_payload(source: Source) -> list[dict]:
    try:
        payload = source.config.to_dict()
    except Exception:
        payload = {}
    return list(payload.get("rules", []) or [])


def _tracked_sources_payload(session: Session, current_source: Source) -> list[dict]:
    tracked: list[dict] = []
    seen_hosts: set[str] = set()

    def add_source(source: Source) -> None:
        host = (urlparse(source.uri).hostname or "").lower()
        if not host or host in seen_hosts:
            return
        tracked.append(
            {
                "id": source.id,
                "uri": source.uri,
                "rules": _source_rules_payload(source),
            }
        )
        seen_hosts.add(host)

    add_source(current_source)
    try:
        sources = (
            session.execute(
                select(Source).where(Source.is_paused == False)  # noqa: E712
            )
            .scalars()
            .all()
        )
        for source in sources:
            add_source(source)
    except Exception:
        pass
    return tracked


def _try_acquire_source_crawl_lock(session: Session, source_id: int) -> bool:
    try:
        result = session.execute(
            text("SELECT pg_try_advisory_lock(:namespace, :source_id)"),
            {"namespace": _CRAWL_LOCK_NAMESPACE, "source_id": source_id},
        )
        return bool(result.scalar_one())
    except Exception:
        return True


def _release_source_crawl_lock(session: Session, source_id: int) -> None:
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:namespace, :source_id)"),
            {"namespace": _CRAWL_LOCK_NAMESPACE, "source_id": source_id},
        )
    except Exception:
        pass


def _find_active_crawl_run(
    session: Session,
    source_id: int,
    *,
    now: datetime,
    max_crawl_age: timedelta,
):
    return session.execute(
        select(CrawlRun).where(
            CrawlRun.source_id == source_id,
            CrawlRun.finished_at.is_(None),
            CrawlRun.started_at >= now - max_crawl_age,
        )
    ).scalar_one_or_none()


def _reserve_source_crawl_run(
    session: Session,
    source_id: int,
    *,
    now: datetime,
    max_crawl_age: timedelta = timedelta(days=30),
) -> int | None:
    if not _try_acquire_source_crawl_lock(session, source_id):
        return None

    try:
        active_run = _find_active_crawl_run(
            session,
            source_id,
            now=now,
            max_crawl_age=max_crawl_age,
        )
        if active_run:
            return None

        session.execute(
            update(CrawlRun)
            .where(
                CrawlRun.source_id == source_id,
                CrawlRun.finished_at.is_(None),
            )
            .values(
                finished_at=now,
                exit_reason="interrupted",
            )
        )

        run = CrawlRun(source_id=source_id, started_at=now)
        session.add(run)
        session.flush()
        session.commit()
        return run.id
    finally:
        _release_source_crawl_lock(session, source_id)


def _mark_crawl_run_finished(
    source_id: int,
    crawl_run_id: int | None,
    *,
    exit_reason: str,
    notes: str | None = None,
) -> None:
    if not crawl_run_id:
        return

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            run = session.get(CrawlRun, crawl_run_id)
            if not run or run.source_id != source_id:
                return
            if run.finished_at is None:
                run.finished_at = datetime.now(timezone.utc)
            run.exit_reason = exit_reason
            if notes:
                run.notes = notes
            session.commit()
    finally:
        engine.dispose()


@app.task(
    name="jobs.crawler.tasks.crawl_source_task",
    queue="celery",
)
def crawl_source_task(source_id: int, skip_sitemap_sync: bool = False):
    print(f"Starting crawl for source {source_id}")

    engine = create_sync_engine()
    crawl_run_id: int | None = None
    try:
        try:
            with Session(bind=engine) as session:
                source = session.get(Source, source_id)
                if not source:
                    print(f"Source {source_id} not found")
                    return

                if source.is_paused:
                    print(f"Source {source_id} is paused, skipping")
                    return
                if source.blocked_reason:
                    print(
                        f"Source {source_id} is blocked ({source.blocked_reason}), skipping"
                    )
                    return

                url = source.uri
                source_title = source.title
                crawler_payload = source.config.to_dict()
                crawler_payload["tracked_sources"] = _tracked_sources_payload(
                    session, source
                )
                crawl_run_id = _reserve_source_crawl_run(
                    session,
                    source_id,
                    now=datetime.now(timezone.utc),
                )
                if crawl_run_id is None:
                    print(
                        f"Source {source_id}: crawl already reserved or running, skipping"
                    )
                    return
                crawler_payload["crawl_run_id"] = crawl_run_id
                _refresh_source_discovery(session, source, crawler_payload)
                if not skip_sitemap_sync:
                    _sync_sitemaps_for_source(session, source_id)

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
                if len(recent_runs) == 3 and all(
                    r.was_rate_limited for r in recent_runs
                ):
                    base_delay = int(
                        float(
                            crawler_payload.get(
                                "crawler_download_delay",
                                DEFAULT_CRAWLER_DOWNLOAD_DELAY,
                            )
                        )
                    )
                    doubled = min(base_delay * 2, 30)
                    crawler_payload["crawler_download_delay"] = doubled
                    print(
                        f"Source {source_id}: 3 consecutive rate-limited runs, "
                        f"doubling download_delay to {doubled}s"
                    )
        except Exception as exc:
            _mark_crawl_run_finished(
                source_id,
                crawl_run_id,
                exit_reason="error",
                notes=f"crawl bootstrap failed: {exc}",
            )
            raise

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

    try:
        result = subprocess.run(
            runner_cmd,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:
        _mark_crawl_run_finished(
            source_id,
            crawl_run_id,
            exit_reason="error",
            notes=f"crawler_runner launch failed: {exc}",
        )
        raise

    if result.returncode != 0:
        print(f"Crawler failed with exit code {result.returncode}")
        _mark_crawl_run_finished(
            source_id,
            crawl_run_id,
            exit_reason="error",
            notes=f"crawler_runner exit code {result.returncode}",
        )
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
            update_inlink_counts_task.si(),
            cleanup_orphans_task.si(),
        ).apply_async()
        schedule_refresh_project_index()
        rebuild_boilerplate_index.delay(source_id)

    print(f"Finished crawling source {source_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_page_task",
    queue="celery",
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
            crawler_payload["tracked_sources"] = _tracked_sources_payload(
                session, source
            )

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
            update_inlink_counts_task.si(),
            cleanup_orphans_task.si(),
        ).apply_async()
        schedule_refresh_project_index()
        rebuild_boilerplate_index.delay(source_id)

    print(f"Finished crawling page {page_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_all_sources_task",
    queue="celery",
)
def crawl_all_sources_task():
    """
    Crawl all non-upload sources.
    """
    print("Starting crawl for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = select(Source).where(  # noqa: E712
                Source.is_paused == False,
                Source.blocked_reason.is_(None),
            )
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


def source_is_due_for_reindex(source: Source, now: datetime) -> bool:
    cron_expression = normalize_reindex_cron(
        getattr(source, "reindex_cron", None) or DEFAULT_REINDEX_CRON
    )
    if is_manual_reindex(cron_expression):
        return False

    minute, hour, day_of_month, month_of_year, day_of_week = cron_expression.split(" ")
    schedule = crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        nowfun=lambda: now,
    )
    last_reference = normalize_datetime(getattr(source, "last_reindexed_at", None))
    created_at = normalize_datetime(getattr(source, "created_at", None))
    baseline = last_reference or created_at or (now - timedelta(days=366))
    if baseline > now:
        baseline = now
    return schedule.is_due(baseline).is_due


@app.task(
    name="jobs.crawler.tasks.schedule_reindex_sources_task",
    queue="celery",
)
def schedule_reindex_sources_task():
    print("Checking sources for scheduled reindex")

    now = datetime.now(timezone.utc)
    # CrawlRun older than this is considered zombie (worker died without closing the run)
    max_crawl_age = timedelta(days=30)
    queued_ids: list[int] = []

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            sources = (
                session.execute(
                    select(Source).where(  # noqa: E712
                        Source.is_paused == False,
                        Source.blocked_reason.is_(None),
                    )
            )
            .scalars()
            .all()
        )

            for source in sources:
                if not source_is_due_for_reindex(source, now):
                    continue

                # Skip if an active (non-zombie) crawl is already running for this source
                active_run = _find_active_crawl_run(
                    session,
                    source.id,
                    now=now,
                    max_crawl_age=max_crawl_age,
                )
                if active_run:
                    print(
                        f"Source {source.id}: crawl already running "
                        f"(run id={active_run.id}, started {active_run.started_at}), skipping"
                    )
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
    queue="celery",
)
def update_inlink_counts_task():
    """Recalculate inlink_count for every page from the PageLink graph."""
    print("Updating inlink counts for all pages")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            updated_pages = session.execute(
                text(
                    """
                    UPDATE page AS p
                    SET inlink_count = COALESCE(link_counts.cnt, 0)
                    FROM page AS p0
                    LEFT JOIN (
                        SELECT
                            target_page_id,
                            COUNT(*)::integer AS cnt
                        FROM page_link
                        WHERE target_page_id IS NOT NULL
                        GROUP BY target_page_id
                    ) AS link_counts
                        ON link_counts.target_page_id = p0.id
                    WHERE p.id = p0.id
                    """
                )
            )
            session.commit()
            updated_count = updated_pages.rowcount or 0
            print(f"Updated inlink counts for {updated_count} pages")
    finally:
        engine.dispose()


@app.task(
    name="jobs.crawler.tasks.cleanup_orphans_task",
    queue="celery",
)
def cleanup_orphans_task():
    """Delete dead pages: http_status 404/410, checked ≥2 times in error, no inlinks."""
    print("Running orphan cleanup for all sources")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            deleted_pages = session.execute(
                text(
                    """
                    DELETE FROM page AS p
                    WHERE p.http_status IN (404, 410)
                      AND p.error_count >= 2
                      AND p.inlink_count = 0
                      AND p.is_hub_page IS FALSE
                    """
                )
            )
            deleted_count = deleted_pages.rowcount or 0
            if deleted_count == 0:
                print("No orphan pages to delete")
                return

            session.commit()
            print(f"Deleted {deleted_count} orphan pages")
    finally:
        engine.dispose()


def _page_filtered_by_source_rules(
    page_url: str | None, source_rules: list[dict] | None
) -> bool:
    if not page_url:
        return False

    normalized_url = normalize_url_for_queue(page_url, source_rules)
    if not normalized_url:
        return False

    if normalized_url != page_url:
        return True

    return not url_allowed_by_rules(normalized_url, source_rules)


@app.task(
    name="jobs.crawler.tasks.reapply_source_rules_task",
    queue="celery",
)
def reapply_source_rules_task(source_id: int) -> int:
    print(f"Reapplying source rules for source {source_id}")

    updated_count = 0
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if not source:
                print(f"Source {source_id} not found, skipping rule reapply")
                return 0

            source_rules = source.config.to_dict().get("rules", [])
            pages = (
                session.execute(select(Page).where(Page.source_id == source_id))
                .scalars()
                .all()
            )

            for page in pages:
                filtered = _page_filtered_by_source_rules(page.uri, source_rules)
                if filtered:
                    if page.status_error != PageStatusError.excluded_rules:
                        page.status = PageStatus.crawler
                        page.status_error = PageStatusError.excluded_rules
                        updated_count += 1
                    continue

                if page.status_error == PageStatusError.excluded_rules:
                    page.status_error = None
                    updated_count += 1

            if updated_count:
                session.commit()

        print(
            f"Reapplied source rules for source {source_id}; updated {updated_count} pages"
        )
        return updated_count
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Sitemap sync
# ---------------------------------------------------------------------------

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_CRAWLER_USER_AGENT = config.get("crawler_user_agent", "Dzen-AI/1.0")


def _fetch_sitemap(
    url: str, last_etag: str | None
) -> tuple[int, bytes | None, str | None, str | None]:
    """
    Fetch a sitemap URL with conditional GET.
    Returns (status_code, body_or_None, etag_or_None).
    304 → body is None.
    """
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    if last_etag:
        headers["If-None-Match"] = last_etag

    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
    except requests.RequestException as exc:
        print(f"Sitemap fetch error {url}: {exc}")
        return 0, None, None, None

    etag = resp.headers.get("ETag")
    location = resp.headers.get("Location")
    if resp.status_code == 304:
        return 304, None, etag, location
    if resp.status_code == 200:
        return 200, resp.content, etag, location
    return resp.status_code, None, etag, location


def _source_host(source_uri: str) -> str:
    return (urlparse(source_uri).hostname or "").lower()


def _is_valid_sitemap_address(source_uri: str, sitemap_url: str) -> bool:
    try:
        parsed = urlparse(sitemap_url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    return (parsed.hostname or "").lower() == _source_host(source_uri)


def _mark_sitemap_ignored(sm: Sitemap, reason: str) -> None:
    sm.is_excluded = True
    sm.ignore_reason = reason


def _parse_robots_txt(body: str) -> tuple[list[str], int | None]:
    sitemap_urls: list[str] = []
    crawl_delay: int | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        if not _:
            continue
        key_norm = key.strip().lower()
        value_norm = value.strip()
        if not value_norm:
            continue
        if key_norm == "sitemap":
            sitemap_urls.append(value_norm)
        elif key_norm == "crawl-delay":
            try:
                crawl_delay = max(crawl_delay or 0, int(float(value_norm)))
            except ValueError:
                continue
    return sitemap_urls, crawl_delay


def _discover_sitemaps_from_robots(source_uri: str) -> tuple[list[str], int | None, str | None]:
    robots_url = urljoin(source_uri.rstrip("/") + "/", "robots.txt")
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    try:
        resp = requests.get(robots_url, headers=headers, timeout=15, allow_redirects=True)
    except requests.RequestException as exc:
        print(f"robots.txt fetch error {robots_url}: {exc}")
        return [], None, None

    if resp.status_code != 200:
        print(f"robots.txt unavailable ({resp.status_code}): {robots_url}")
        return [], None, None

    body = resp.text
    sitemap_urls, crawl_delay = _parse_robots_txt(body)
    return sitemap_urls, crawl_delay, body


def _probe_common_sitemaps(source_uri: str) -> list[str]:
    discovered: list[str] = []
    headers = {"User-Agent": _CRAWLER_USER_AGENT}
    for path in _AUTO_SITEMAP_PATHS:
        url = urljoin(source_uri.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "xml" in content_type or b"<urlset" in resp.content[:256] or b"<sitemapindex" in resp.content[:256]:
            discovered.append(url)
    return discovered


def _upsert_sitemap(
    session: Session,
    *,
    source_id: int,
    sitemap_url: str,
    discovered_via: str,
    discovered_from_url: str | None = None,
    is_excluded: bool = False,
    ignore_reason: str | None = None,
) -> None:
    existing = session.execute(
        select(Sitemap).where(
            Sitemap.source_id == source_id,
            Sitemap.url == sitemap_url,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Sitemap(
                source_id=source_id,
                url=sitemap_url,
                discovered_via=discovered_via,
                discovered_from_url=discovered_from_url,
                is_excluded=is_excluded,
                ignore_reason=ignore_reason,
            )
        )
        return

    if discovered_from_url and not existing.discovered_from_url:
        existing.discovered_from_url = discovered_from_url
    if ignore_reason:
        existing.is_excluded = True
        existing.ignore_reason = ignore_reason


def _refresh_source_discovery(
    session: Session,
    source: Source,
    crawler_payload: dict,
) -> None:
    now = datetime.now(timezone.utc)
    cached = source.robots_cache or {}
    cached_at_raw = cached.get("fetched_at")
    cached_at = None
    if isinstance(cached_at_raw, str):
        try:
            cached_at = datetime.fromisoformat(cached_at_raw)
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
        except ValueError:
            cached_at = None

    sitemap_urls: list[str] = []
    crawl_delay: int | None = None
    robots_body: str | None = None

    if cached_at and (now - cached_at) < _ROBOTS_CACHE_TTL:
        sitemap_urls = list(cached.get("sitemaps") or [])
        crawl_delay = cached.get("crawl_delay")
    else:
        sitemap_urls, crawl_delay, robots_body = _discover_sitemaps_from_robots(source.uri)
        source.robots_cache = {
            "fetched_at": now.isoformat(),
            "sitemaps": sitemap_urls,
            "crawl_delay": crawl_delay,
            "body": robots_body,
        }

    if crawl_delay:
        current_delay = int(float(crawler_payload.get("crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY)))
        crawler_payload["crawler_download_delay"] = max(current_delay, crawl_delay)

    discovered_urls = [url for url in sitemap_urls if url]
    if not discovered_urls:
        known_count = session.execute(
            select(func.count(Sitemap.id)).where(Sitemap.source_id == source.id)
        ).scalar_one()
        if known_count == 0:
            discovered_urls.extend(_probe_common_sitemaps(source.uri))

    for sitemap_url in discovered_urls:
        normalized_sitemap_url = normalize_url_for_queue(sitemap_url)
        if not normalized_sitemap_url:
            continue
        if not _is_valid_sitemap_address(source.uri, normalized_sitemap_url):
            _upsert_sitemap(
                session,
                source_id=source.id,
                sitemap_url=normalized_sitemap_url,
                discovered_via="robots_txt" if sitemap_url in sitemap_urls else "auto_probe",
                is_excluded=True,
                ignore_reason=SITEMAP_IGNORE_WRONG_ADDRESS,
            )
            continue
        _upsert_sitemap(
            session,
            source_id=source.id,
            sitemap_url=normalized_sitemap_url,
            discovered_via="robots_txt" if sitemap_url in sitemap_urls else "auto_probe",
        )


def _upsert_sitemap_pages(
    session: Session,
    *,
    source_id: int,
    parsed_entries: list[tuple[str, str | None]],
    source_rules: list[dict] | None = None,
    source_id_by_host: dict[str, int] | None = None,
) -> set[str]:
    prioritized_urls: set[str] = set()
    for raw_page_url, lastmod_str in parsed_entries:
        page_url = normalize_url_for_queue(raw_page_url, source_rules)
        if not page_url or not url_allowed_by_rules(page_url, source_rules):
            continue
        page_source_id = resolve_source_id_for_url(
            page_url,
            source_id_by_host or {},
            fallback_source_id=source_id,
        )
        page = session.execute(select(Page).where(Page.uri == page_url)).scalar_one_or_none()
        if page is None:
            page = Page(source_id=page_source_id, uri=page_url)
            page._hash = ""
            session.add(page)
            continue

        if lastmod_str is None:
            continue
        try:
            new_lastmod = normalize_datetime(
                datetime.fromisoformat(lastmod_str.replace("Z", "+00:00"))
            )
        except ValueError:
            continue
        existing_lastmod = normalize_datetime(page.last_modified_at)
        if existing_lastmod is None:
            prioritized_urls.add(page_url)
            continue
        if existing_lastmod < new_lastmod:
            prioritized_urls.add(page_url)
    return prioritized_urls


def _upsert_child_sitemaps(
    session: Session,
    *,
    source_id: int,
    source_uri: str,
    parent_sitemap_url: str,
    parsed_entries: list[tuple[str, str | None]],
) -> list[str]:
    discovered_urls: list[str] = []
    for raw_sitemap_url, _lastmod_str in parsed_entries:
        sitemap_url = normalize_url_for_queue(raw_sitemap_url)
        if not sitemap_url:
            continue
        if not _is_valid_sitemap_address(source_uri, sitemap_url):
            _upsert_sitemap(
                session,
                source_id=source_id,
                sitemap_url=sitemap_url,
                discovered_via="sitemap_index",
                discovered_from_url=parent_sitemap_url,
                is_excluded=True,
                ignore_reason=SITEMAP_IGNORE_WRONG_ADDRESS,
            )
            continue
        _upsert_sitemap(
            session,
            source_id=source_id,
            sitemap_url=sitemap_url,
            discovered_via="sitemap_index",
            discovered_from_url=parent_sitemap_url,
        )
        discovered_urls.append(sitemap_url)
    return discovered_urls


def _sync_sitemaps_for_source(session: Session, source_id: int) -> None:
    source = session.get(Source, source_id)
    if source is None:
        print(f"Source {source_id} not found during sitemap sync")
        return
    source_rules = []
    source_rules = [rule.to_dict() for rule in source.config.rules]
    source_rows = session.execute(select(Source.id, Source.uri)).all()
    source_id_by_host = build_source_id_by_host(source_rows)

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
    pending_sitemaps = list(sitemaps)
    seen_sitemaps: set[str] = set()

    while pending_sitemaps:
        sm = pending_sitemaps.pop(0)
        if sm.url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm.url)

        if not _is_valid_sitemap_address(source.uri, sm.url):
            _mark_sitemap_ignored(sm, SITEMAP_IGNORE_WRONG_ADDRESS)
            print(f"Sitemap ignored (wrong address): {sm.url}")
            continue

        now = datetime.now(timezone.utc)
        if (
            sm.last_fetched_at is not None
            and sm.last_fetched_at > now - _SITEMAP_FETCH_INTERVAL
        ):
            print(f"Sitemap fetch skipped (<24h since last check): {sm.url}")
            continue

        status_code, body, etag, location = _fetch_sitemap(sm.url, sm.last_etag)

        if status_code == 304:
            sm.last_fetched_at = now
            if etag:
                sm.last_etag = etag
            print(f"Sitemap unchanged (304): {sm.url}")
            continue

        if 300 <= status_code < 400:
            _mark_sitemap_ignored(sm, SITEMAP_IGNORE_REDIRECT)
            print(f"Sitemap ignored (redirect to {location}): {sm.url}")
            continue

        if status_code != 200 or body is None:
            _mark_sitemap_ignored(sm, f"{SITEMAP_IGNORE_HTTP_ERROR_PREFIX}{status_code}")
            print(f"Sitemap fetch failed ({status_code}): {sm.url}")
            continue

        if not body.strip():
            _mark_sitemap_ignored(sm, SITEMAP_IGNORE_EMPTY_BODY)
            print(f"Sitemap ignored (empty body): {sm.url}")
            continue

        sm.ignore_reason = None

        new_hash = hashlib.sha256(body).hexdigest()

        if new_hash == sm.last_content_hash:
            sm.last_fetched_at = now
            if etag:
                sm.last_etag = etag
            print(f"Sitemap hash unchanged: {sm.url}")
            continue

        document_kind, parsed_entries = _parse_sitemap_document(body)
        if document_kind == "sitemapindex":
            discovered_urls = _upsert_child_sitemaps(
                session,
                source_id=source_id,
                source_uri=source.uri,
                parent_sitemap_url=sm.url,
                parsed_entries=parsed_entries,
            )
            if discovered_urls:
                session.flush()
                child_sitemaps = (
                    session.execute(
                        select(Sitemap).where(
                            Sitemap.source_id == source_id,
                            Sitemap.url.in_(discovered_urls),
                        )
                    )
                    .scalars()
                    .all()
                )
                pending_sitemaps.extend(child_sitemaps)
        else:
            prioritized_urls.update(
                _upsert_sitemap_pages(
                    session,
                    source_id=source_id,
                    parsed_entries=parsed_entries,
                    source_rules=source_rules,
                    source_id_by_host=source_id_by_host,
                )
            )

        sm.last_content_hash = new_hash
        sm.last_fetched_at = now
        sm.url_count = len(parsed_entries)
        if etag:
            sm.last_etag = etag
        print(
            f"Sitemap updated: {sm.url} ({len(parsed_entries)} {document_kind} entries)"
        )

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
        print(f"Prioritized {len(pages_to_prioritize)} pages with changed lastmod")


def _parse_sitemap_document(body: bytes) -> tuple[str, list[tuple[str, str | None]]]:
    """
    Parse sitemap XML and return:
    - document kind: "urlset" or "sitemapindex"
    - list of (url, lastmod_or_None)
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "unknown", []

    results: list[tuple[str, str | None]] = []
    root_tag = root.tag.rsplit("}", 1)[-1]
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

    if root_tag == "sitemapindex":
        return "sitemapindex", results
    return "urlset", results


@app.task(
    name="jobs.crawler.tasks.sitemap_sync_task",
    queue="celery",
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
            source = session.get(Source, source_id)
            if source is None:
                print(f"Source {source_id} not found during sitemap sync")
                return
            if source.blocked_reason:
                print(
                    f"Source {source_id} is blocked ({source.blocked_reason}), sitemap sync skipped"
                )
                return
            _sync_sitemaps_for_source(session, source_id)
            session.commit()
    finally:
        engine.dispose()

    print(f"Sitemap sync complete for source {source_id}")


@app.task(
    name="jobs.crawler.tasks.schedule_sitemap_sync_task",
    queue="celery",
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
                    select(Source.id).where(
                        Source.is_paused.is_(False),
                        Source.blocked_reason.is_(None),
                    )
                ).all()
            ]
    finally:
        engine.dispose()

    for source_id in source_ids:
        sitemap_sync_task.delay(source_id)


def refresh_source_blocking_state(source_id: int) -> bool:
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.get(Source, source_id)
            if source is None:
                return False
            result = check_source_blocking(source.uri)
            apply_source_blocking_result(source, result)
            source.updated_at = datetime.now(timezone.utc)
            session.commit()
            return result.is_blocked
    finally:
        engine.dispose()

    print(f"Queued sitemap sync for {len(source_ids)} sources")
