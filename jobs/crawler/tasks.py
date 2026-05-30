import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery.schedules import crontab
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.embedder.tasks import refresh_project_index
from vchat.models.data import Source, CrawlRun
from vchat.source_settings import (
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
        finally:
            engine.dispose()

        print("Triggering refresh_project_index")
        refresh_project_index.delay()

    print(f"Finished crawling source {source_id}")


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
