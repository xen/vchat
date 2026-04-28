from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jobs.crawler import tasks as crawler_tasks
from vchat.source_settings import normalize_reindex_cron, validate_reindex_cron


class _Engine:
    def dispose(self):
        return None


class _SessionCtx:
    def __init__(self, sources):
        self._sources = sources

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, stmt):
        _ = stmt
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
    assert crawler_tasks._cron_matches_now(cron_expression, now) is should_match


def test_manual_reindex_mode_helpers() -> None:
    assert normalize_reindex_cron("") == "manual"
    assert normalize_reindex_cron("manual") == "manual"
    assert validate_reindex_cron("") is True
    assert validate_reindex_cron("manual") is True


@pytest.mark.asyncio
async def test_schedule_reindex_sources_task_uses_cron_and_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    matching_cron = f"{now.minute} {now.hour} * * *"
    non_matching_minute = (now.minute + 1) % 60
    non_matching_cron = f"{non_matching_minute} {now.hour} * * *"

    sources = [
        SimpleNamespace(
            id=1,
            type="site",
            reindex_cron=matching_cron,
            last_reindexed_at=now - timedelta(days=2),
        ),
        SimpleNamespace(
            id=2,
            type="sitemap",
            reindex_cron=matching_cron,
            last_reindexed_at=now - timedelta(hours=12),
        ),
        SimpleNamespace(
            id=3,
            type="list",
            reindex_cron=non_matching_cron,
            last_reindexed_at=None,
        ),
        SimpleNamespace(
            id=4,
            type="site",
            reindex_cron=matching_cron,
            last_reindexed_at=None,
        ),
        SimpleNamespace(
            id=5,
            type="site",
            reindex_cron="manual",
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
        crawler_tasks, "Session", lambda bind=None: _SessionCtx(sources)
    )
    monkeypatch.setattr(
        crawler_tasks.crawl_source_task,
        "delay",
        lambda source_id: delayed_ids.append(source_id),
    )

    crawler_tasks.schedule_reindex_sources_task()

    assert delayed_ids == [1, 4]
