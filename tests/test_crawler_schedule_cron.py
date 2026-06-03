from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jobs.crawler import tasks as crawler_tasks
from jobs.celery import app as celery_app
from vchat.source_settings import normalize_reindex_cron, validate_reindex_cron


class _Engine:
    def dispose(self):
        return None


class _SessionCtx:
    def __init__(self, sources, active_run_ids: set[int] | None = None):
        self._sources = sources
        self._active_run_ids: set[int] = active_run_ids or set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, stmt):
        from sqlalchemy.dialects import sqlite

        try:
            compiled = stmt.compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
            )
            stmt_str = str(compiled)
        except Exception:
            stmt_str = str(stmt)

        if "crawl_run" in stmt_str.lower():
            active_run = None
            m = re.search(r"source_id\s*=\s*(\d+)", stmt_str)
            if m:
                queried_id = int(m.group(1))
                if queried_id in self._active_run_ids:
                    active_run = SimpleNamespace(id=queried_id, started_at=None)
            return SimpleNamespace(scalar_one_or_none=lambda: active_run)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self._sources)
        )


@pytest.mark.parametrize(
    ("cron_expression", "should_match"),
    [
        ("* * * * *", True),
        ("0 0 1 1 *", False),
        ("invalid", False),
    ],
)
def test_cron_matches_now(cron_expression: str, should_match: bool) -> None:
    now = datetime.now(timezone.utc)
    assert crawler_tasks.cron_matches_now(cron_expression, now) is should_match


def test_manual_reindex_mode_helpers() -> None:
    assert normalize_reindex_cron("") == "manual"
    assert normalize_reindex_cron("manual") == "manual"
    assert validate_reindex_cron("") is True
    assert validate_reindex_cron("manual") is True


def test_sitemap_sync_schedule_is_daily() -> None:
    entry = celery_app.conf.beat_schedule["schedule_sitemap_sync"]
    schedule = entry["schedule"]
    assert schedule._orig_minute == 0
    assert schedule._orig_hour == 3


def test_reindex_schedule_is_hourly() -> None:
    entry = celery_app.conf.beat_schedule["schedule_source_reindex"]
    schedule = entry["schedule"]
    assert schedule._orig_minute == 0


def test_source_is_due_for_reindex_uses_last_reindexed_at() -> None:
    now = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
    source = SimpleNamespace(
        reindex_cron="15 9 * * *",
        created_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        last_reindexed_at=datetime(2026, 6, 2, 8, 0, tzinfo=timezone.utc),
    )
    assert crawler_tasks.source_is_due_for_reindex(source, now) is True


def test_source_is_not_due_if_already_reindexed_after_latest_slot() -> None:
    now = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
    source = SimpleNamespace(
        reindex_cron="15 9 * * *",
        created_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        last_reindexed_at=datetime(2026, 6, 3, 9, 30, tzinfo=timezone.utc),
    )
    assert crawler_tasks.source_is_due_for_reindex(source, now) is False


@pytest.mark.asyncio
async def test_schedule_reindex_sources_task_skips_active_and_non_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    matching_cron = f"{now.minute} {now.hour} * * *"
    non_matching_minute = (now.minute + 1) % 60
    non_matching_cron = f"{non_matching_minute} {now.hour} * * *"

    sources = [
        # id=1: matching cron, no active run → queued
        SimpleNamespace(
            id=1,
            reindex_cron=matching_cron,
            created_at=now - timedelta(days=10),
            last_reindexed_at=now - timedelta(days=1),
        ),
        # id=2: matching cron, active CrawlRun → skipped (already running)
        SimpleNamespace(
            id=2,
            reindex_cron=matching_cron,
            created_at=now - timedelta(days=10),
            last_reindexed_at=now - timedelta(days=1),
        ),
        # id=3: non-matching cron → skipped
        SimpleNamespace(
            id=3,
            reindex_cron=non_matching_cron,
            created_at=now - timedelta(days=10),
            last_reindexed_at=now,
        ),
        # id=4: matching cron, no active run → queued
        SimpleNamespace(
            id=4,
            reindex_cron=matching_cron,
            created_at=now - timedelta(days=10),
            last_reindexed_at=now - timedelta(days=2),
        ),
        # id=5: manual reindex → skipped
        SimpleNamespace(
            id=5,
            reindex_cron="manual",
            created_at=now - timedelta(days=10),
            last_reindexed_at=None,
        ),
    ]

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            _ = tz
            return now

    delayed_ids: list[int] = []

    monkeypatch.setattr(crawler_tasks, "datetime", _FrozenDateTime)
    monkeypatch.setattr(crawler_tasks, "create_sync_engine", lambda: _Engine())
    monkeypatch.setattr(
        crawler_tasks,
        "Session",
        lambda bind=None: _SessionCtx(sources, active_run_ids={2}),
    )
    monkeypatch.setattr(
        crawler_tasks.crawl_source_task,
        "delay",
        lambda source_id: delayed_ids.append(source_id),
    )

    crawler_tasks.schedule_reindex_sources_task()

    assert delayed_ids == [1, 4]
