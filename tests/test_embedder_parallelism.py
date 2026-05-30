from __future__ import annotations

import pytest

from jobs.embedder import launcher
from jobs.embedder import tasks as embedder_tasks


def test_pending_chunk_task_target_respects_batch_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks, "PENDING_CHUNKS_BATCH_SIZE", 8)
    monkeypatch.setattr(embedder_tasks, "PENDING_CHUNKS_MAX_INFLIGHT", 4)

    assert embedder_tasks._pending_chunk_task_target(0) == 0
    assert embedder_tasks._pending_chunk_task_target(1) == 1
    assert embedder_tasks._pending_chunk_task_target(8) == 1
    assert embedder_tasks._pending_chunk_task_target(9) == 2
    assert embedder_tasks._pending_chunk_task_target(99) == 4


def test_run_pending_chunk_batch_stops_at_batch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks, "PENDING_CHUNKS_BATCH_SIZE", 2)
    outcomes = iter([True, True, True])
    calls: list[tuple[object, object]] = []

    def _process(session, redis_client=None):
        calls.append((session, redis_client))
        return next(outcomes)

    monkeypatch.setattr(embedder_tasks, "_process_next_pending_chunk", _process)
    monkeypatch.setattr(embedder_tasks, "_count_pending_chunks", lambda session: 7)

    processed, remaining = embedder_tasks._run_pending_chunk_batch(
        session="db-session",
        redis_client="redis-client",
    )

    assert processed == 2
    assert remaining == 7
    assert calls == [
        ("db-session", "redis-client"),
        ("db-session", "redis-client"),
    ]


def test_ensure_pending_chunk_workers_schedules_only_missing_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks, "_count_pending_chunks", lambda session: 19)
    reserved_targets: list[int] = []
    scheduled_counts: list[int] = []

    def _reserve(_redis_client, target):
        reserved_targets.append(target)
        return 2

    monkeypatch.setattr(embedder_tasks, "_reserve_pending_chunk_slots", _reserve)
    monkeypatch.setattr(
        embedder_tasks,
        "_schedule_pending_chunk_tasks",
        lambda count: scheduled_counts.append(count) or count,
    )
    monkeypatch.setattr(
        embedder_tasks,
        "_release_pending_chunk_slots",
        lambda _redis_client, slots=1: (_redis_client, slots),
    )
    monkeypatch.setattr(embedder_tasks, "PENDING_CHUNKS_BATCH_SIZE", 8)
    monkeypatch.setattr(embedder_tasks, "PENDING_CHUNKS_MAX_INFLIGHT", 4)

    pending, scheduled = embedder_tasks._ensure_pending_chunk_workers(
        session="db-session",
        redis_client="redis-client",
    )

    assert pending == 19
    assert scheduled == 2
    assert reserved_targets == [3]
    assert scheduled_counts == [2]


def test_schedule_ensure_pending_chunks_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int | None, bool | None]] = []
    deleted: list[str] = []
    closed: list[bool] = []
    delayed: list[bool] = []

    class _Redis:
        def __init__(self, acquired: bool):
            self.acquired = acquired

        def set(self, key, value, ex=None, nx=None):
            calls.append((key, ex, nx))
            return self.acquired

        def delete(self, key):
            deleted.append(key)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        embedder_tasks.redis,
        "from_url",
        lambda _url: _Redis(acquired=True),
    )
    monkeypatch.setattr(
        embedder_tasks,
        "ensure_pending_chunks",
        type("_Task", (), {"delay": staticmethod(lambda: delayed.append(True))}),
    )

    assert embedder_tasks._schedule_ensure_pending_chunks() is True
    assert delayed == [True]
    assert deleted == []
    assert closed == [True]
    assert calls == [
        (embedder_tasks.ENSURE_PENDING_CHUNKS_SCHEDULE_KEY, 120, True),
    ]


def test_schedule_ensure_pending_chunks_skips_when_already_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class _Redis:
        def set(self, key, value, ex=None, nx=None):
            _ = key, value, ex, nx
            return False

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        embedder_tasks.redis,
        "from_url",
        lambda _url: _Redis(),
    )
    monkeypatch.setattr(
        embedder_tasks,
        "ensure_pending_chunks",
        type("_Task", (), {"delay": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError))}),
    )

    assert embedder_tasks._schedule_ensure_pending_chunks() is False
    assert closed == [True]


def test_schedule_index_document_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int | None, bool | None]] = []
    delayed: list[int] = []
    deleted: list[str] = []
    closed: list[bool] = []

    class _Redis:
        def __init__(self, acquired: bool):
            self.acquired = acquired

        def set(self, key, value, ex=None, nx=None):
            _ = value
            calls.append((key, ex, nx))
            return self.acquired

        def delete(self, key):
            deleted.append(key)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        embedder_tasks.redis,
        "from_url",
        lambda _url: _Redis(acquired=True),
    )
    monkeypatch.setattr(
        embedder_tasks,
        "index_document",
        type("_Task", (), {"delay": staticmethod(lambda doc_id: delayed.append(doc_id))}),
    )

    assert embedder_tasks.schedule_index_document(77) is True
    assert delayed == [77]
    assert deleted == []
    assert calls == [
        (
            f"{embedder_tasks.INDEX_DOCUMENT_SCHEDULE_KEY_PREFIX}77",
            embedder_tasks.INDEX_DOCUMENT_SCHEDULE_TTL,
            True,
        )
    ]
    assert closed == [True]


def test_schedule_index_document_skips_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class _Redis:
        def set(self, key, value, ex=None, nx=None):
            _ = key, value, ex, nx
            return False

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        embedder_tasks.redis,
        "from_url",
        lambda _url: _Redis(),
    )
    monkeypatch.setattr(
        embedder_tasks,
        "index_document",
        type("_Task", (), {"delay": staticmethod(lambda doc_id: (_ for _ in ()).throw(RuntimeError(doc_id)))}),
    )

    assert embedder_tasks.schedule_index_document(77) is False
    assert closed == [True]


def test_resolve_embedder_instance_count_auto_for_cpu() -> None:
    assert (
        launcher.resolve_embedder_instance_count(
            configured="auto",
            cpu_count=8,
            reserve_cpus=2,
            device="cpu",
        )
        == 6
    )


def test_resolve_embedder_instance_count_auto_keeps_single_gpu_worker() -> None:
    assert (
        launcher.resolve_embedder_instance_count(
            configured="auto",
            cpu_count=16,
            reserve_cpus=0,
            device="mps",
        )
        == 1
    )


def test_resolve_embedder_instance_count_honors_explicit_override() -> None:
    assert launcher.resolve_embedder_instance_count(configured="5", device="cpu") == 5
