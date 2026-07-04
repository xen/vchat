from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

from jobs.crawler import tasks as crawler_tasks
from jobs.embedder import launcher
from jobs.embedder import tasks as embedder_tasks
from jobs.embedder import queue as embedding_queue


def test_pending_chunk_task_target_respects_batch_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_queue.cfg, "embedding_pending_chunks_batch_size", 8)
    monkeypatch.setattr(embedding_queue.cfg, "embedding_pending_chunks_max_inflight", 4)

    assert embedding_queue.pending_chunk_task_target(0) == 0
    assert embedding_queue.pending_chunk_task_target(1) == 1
    assert embedding_queue.pending_chunk_task_target(8) == 1
    assert embedding_queue.pending_chunk_task_target(9) == 2
    assert embedding_queue.pending_chunk_task_target(99) == 4


def test_run_pending_chunk_batch_stops_at_batch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks.cfg, "embedding_pending_chunks_batch_size", 2)
    calls: list[tuple[object, int]] = []

    def _process(session, *, batch_size):
        calls.append((session, batch_size))
        return 2

    monkeypatch.setattr(embedder_tasks, "process_pending_chunk_batch", _process)
    monkeypatch.setattr(embedder_tasks, "pending_chunks_remain", lambda session: True)

    processed, remaining = embedder_tasks.run_pending_chunk_batch(
        session="db-session",
    )

    assert processed == 2
    assert remaining == 1
    assert calls == [("db-session", 2)]


def test_apply_embedding_to_matching_kb_chunks_updates_by_hash_and_normalized_text():
    calls = []

    class _Result:
        def all(self):
            return [(10,), (11,)]

    class _Session:
        def execute(self, stmt):
            calls.append(str(stmt.compile(dialect=postgresql.dialect())))
            return _Result()

    updated_page_ids = embedder_tasks.apply_embedding_to_matching_kb_chunks(
        _Session(),
        text_hash="abc123",
        text="\u200b  shared text \ufeff",
        embedding=[0.1, 0.2],
    )

    assert updated_page_ids == {10, 11}
    assert calls
    assert "FOR UPDATE SKIP LOCKED" in calls[0]
    assert "chunk.embedding IS NULL" in calls[0]
    assert "chunk.text_hash =" in calls[0]
    assert "btrim(translate(chunk.text" in calls[0]


def test_ensure_pending_chunk_workers_schedules_only_missing_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_queue, "count_pending_chunks", lambda session: 19)
    reserved_targets: list[int] = []
    scheduled_counts: list[int] = []

    def _reserve(_redis_client, target):
        reserved_targets.append(target)
        return 2

    monkeypatch.setattr(embedding_queue, "reserve_pending_chunk_slots", _reserve)
    monkeypatch.setattr(
        embedding_queue,
        "release_pending_chunk_slots",
        lambda _redis_client, slots=1: (_redis_client, slots),
    )
    monkeypatch.setattr(embedding_queue.cfg, "embedding_pending_chunks_batch_size", 8)
    monkeypatch.setattr(embedding_queue.cfg, "embedding_pending_chunks_max_inflight", 4)

    pending, scheduled = embedding_queue.ensure_pending_chunk_workers(
        session="db-session",
        redis_client="redis-client",
        schedule_tasks=lambda count: scheduled_counts.append(count) or count,
    )

    assert pending == 19
    assert scheduled == 2
    assert reserved_targets == [3]
    assert scheduled_counts == [2]


def test_schedule_ensure_pending_chunks_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        crawler_tasks.redis,
        "from_url",
        lambda _url: _Redis(acquired=True),
    )
    monkeypatch.setattr(
        crawler_tasks,
        "ensure_pending_chunks",
        type("_Task", (), {"delay": staticmethod(lambda: delayed.append(True))}),
    )

    assert crawler_tasks.schedule_ensure_pending_chunks() is True
    assert delayed == [True]
    assert deleted == []
    assert closed == [True]
    assert calls == [
        (crawler_tasks.ENSURE_PENDING_CHUNKS_SCHEDULE_KEY, 120, True),
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
        crawler_tasks.redis,
        "from_url",
        lambda _url: _Redis(),
    )
    monkeypatch.setattr(
        crawler_tasks,
        "ensure_pending_chunks",
        type(
            "_Task",
            (),
            {"delay": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError))},
        ),
    )

    assert crawler_tasks.schedule_ensure_pending_chunks() is False
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
        crawler_tasks.redis,
        "from_url",
        lambda _url: _Redis(acquired=True),
    )
    monkeypatch.setattr(
        crawler_tasks,
        "index_document",
        type(
            "_Task", (), {"delay": staticmethod(lambda doc_id: delayed.append(doc_id))}
        ),
    )

    assert crawler_tasks.schedule_index_document(77) is True
    assert delayed == [77]
    assert deleted == []
    assert calls == [
        (
            f"{crawler_tasks.INDEX_DOCUMENT_SCHEDULE_KEY_PREFIX}77",
            crawler_tasks.cfg.embedding_index_document_schedule_ttl_seconds,
            True,
        )
    ]
    assert closed == [True]


def test_schedule_index_document_skips_duplicate(
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
        crawler_tasks.redis,
        "from_url",
        lambda _url: _Redis(),
    )
    monkeypatch.setattr(
        crawler_tasks,
        "index_document",
        type(
            "_Task",
            (),
            {
                "delay": staticmethod(
                    lambda doc_id: (_ for _ in ()).throw(RuntimeError(doc_id))
                )
            },
        ),
    )

    assert crawler_tasks.schedule_index_document(77) is False
    assert closed == [True]


def test_index_page_inner_skips_when_document_lock_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace(id=77)
    rollbacks: list[bool] = []

    monkeypatch.setattr(
        crawler_tasks, "fetch_page_context", lambda _session, _page_id: context
    )
    monkeypatch.setattr(
        crawler_tasks, "_try_acquire_document_index_lock", lambda *_args: False
    )
    monkeypatch.setattr(
        crawler_tasks,
        "index_page_chunks",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("must not materialize")),
    )

    session = SimpleNamespace(rollback=lambda: rollbacks.append(True))

    assert crawler_tasks.index_page_inner(session, 77) is False
    assert rollbacks == [True]


def test_index_page_inner_skips_current_chunks_and_reschedules_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace(id=77)
    scheduled: list[bool] = []
    commits: list[bool] = []

    class _Result:
        def scalar_one(self):
            return 3

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return _Result()

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(
        crawler_tasks, "fetch_page_context", lambda _session, _page_id: context
    )
    monkeypatch.setattr(
        crawler_tasks, "_try_acquire_document_index_lock", lambda *_args: True
    )
    monkeypatch.setattr(
        crawler_tasks, "page_chunks_match_current_content", lambda *_args: True
    )
    monkeypatch.setattr(
        crawler_tasks,
        "schedule_ensure_pending_chunks",
        lambda: scheduled.append(True) or True,
    )
    monkeypatch.setattr(
        crawler_tasks,
        "index_page_chunks",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("must not materialize")),
    )

    assert crawler_tasks.index_page_inner(_Session(), 77) is True
    assert scheduled == [True]
    assert commits == [True]


def test_index_page_inner_materializes_when_chunks_are_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace(id=77)
    materialized: list[tuple[object, object]] = []

    monkeypatch.setattr(
        crawler_tasks, "fetch_page_context", lambda _session, _page_id: context
    )
    monkeypatch.setattr(
        crawler_tasks, "_try_acquire_document_index_lock", lambda *_args: True
    )
    monkeypatch.setattr(
        crawler_tasks, "page_chunks_match_current_content", lambda *_args: False
    )
    monkeypatch.setattr(
        crawler_tasks,
        "index_page_chunks",
        lambda session, doc: materialized.append((session, doc)) or True,
    )

    session = object()

    assert crawler_tasks.index_page_inner(session, 77) is True
    assert materialized == [(session, context)]


def test_make_embed_vectors_splits_large_encode_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, bool]] = []

    class _Model:
        def encode(
            self,
            texts,
            normalize_embeddings=True,
            batch_size=1,
            show_progress_bar=True,
        ):
            _ = normalize_embeddings
            calls.append((list(texts), batch_size, show_progress_bar))
            return np.array(
                [[float(index)] for index, _text in enumerate(texts)],
                dtype=np.float32,
            )

    monkeypatch.setattr(embedder_tasks.cfg, "embedding_encode_batch_max_chars", 10)
    monkeypatch.setattr(embedder_tasks, "get_embed_model", lambda: _Model())

    vectors = embedder_tasks.make_embed_vectors(["1234", "5678", "abcdef", "gh"])

    assert vectors == [[0.0], [1.0], [0.0], [1.0]]
    assert calls == [
        (["1234", "5678"], 2, False),
        (["abcdef", "gh"], 2, False),
    ]


def test_process_pending_chunk_batch_embeds_duplicate_text_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            id=1,
            page_id=10,
            chunk_ix=0,
            text="shared text",
            text_hash="hash-1",
            token_count=2,
        ),
        SimpleNamespace(
            id=2,
            page_id=11,
            chunk_ix=0,
            text="\u200bshared text\ufeff",
            text_hash="hash-1",
            token_count=2,
        ),
    ]
    execute_calls = []
    encoded_texts = []
    applied = []
    completed_inputs = []
    commits = []

    class _Result:
        def all(self):
            return rows

    class _Session:
        def execute(self, stmt, params=None):
            execute_calls.append((stmt, params))
            return _Result()

        def commit(self):
            commits.append(True)

        def expunge_all(self):
            pass

    monkeypatch.setattr(
        embedder_tasks,
        "make_embed_vectors",
        lambda texts: encoded_texts.append(list(texts)) or [[0.25]],
    )
    monkeypatch.setattr(
        embedder_tasks,
        "apply_embedding_to_matching_kb_chunks",
        lambda session, *, text_hash, text, embedding: applied.append(
            (session, text_hash, text, embedding)
        )
        or {10, 11},
    )
    monkeypatch.setattr(
        embedder_tasks,
        "mark_completed_pages",
        lambda session, page_ids: completed_inputs.append((session, page_ids))
        or [10, 11],
    )
    monkeypatch.setattr(embedder_tasks, "release_torch_cache", lambda: None)
    monkeypatch.setattr(
        embedder_tasks, "maybe_reset_embed_model_after_document", lambda: False
    )

    processed = embedder_tasks.process_pending_chunk_batch(
        _Session(),
        batch_size=10,
    )

    assert processed == 2
    assert encoded_texts == [["shared text"]]
    assert len(applied) == 1
    assert applied[0][1:] == ("hash-1", "shared text", [0.25])
    assert completed_inputs[0][1] == {10, 11}
    assert commits == [True, True]


def test_make_embed_vector_disables_progress_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, bool]] = []

    class _Model:
        def encode(
            self,
            texts,
            normalize_embeddings=True,
            batch_size=1,
            show_progress_bar=True,
        ):
            _ = normalize_embeddings
            calls.append((list(texts), batch_size, show_progress_bar))
            return np.array([[0.25]], dtype=np.float32)

    monkeypatch.setattr(embedder_tasks, "get_embed_model", lambda: _Model())

    assert embedder_tasks.make_embed_vector("payload") == [0.25]
    assert calls == [(["payload"], 1, False)]


def test_embedding_result_to_vectors_rejects_nan() -> None:
    with pytest.raises(ValueError, match="NaN"):
        embedder_tasks.embedding_result_to_vectors(
            np.array([[0.1, np.nan]], dtype=np.float32)
        )


def test_resolve_embedder_instance_count_auto_for_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.cfg, "embedding_worker_instances", "auto")
    monkeypatch.setattr(launcher.cfg, "embedding_worker_cpu_reserve", 2)
    monkeypatch.setattr(launcher.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(launcher, "resolve_embedding_device", lambda: "cpu")

    assert launcher.resolve_embedder_instance_count() == 6


def test_resolve_embedder_instance_count_auto_keeps_single_gpu_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.cfg, "embedding_worker_instances", "auto")
    monkeypatch.setattr(launcher.cfg, "embedding_worker_cpu_reserve", 0)
    monkeypatch.setattr(launcher.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(launcher, "resolve_embedding_device", lambda: "mps")

    assert launcher.resolve_embedder_instance_count() == 1


def test_resolve_embedder_instance_count_honors_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.cfg, "embedding_worker_instances", 5)

    assert launcher.resolve_embedder_instance_count() == 5


def test_maybe_reset_embed_model_after_document_respects_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks.cfg, "embedding_model_reset_after_documents", 2)
    monkeypatch.setattr(embedder_tasks, "_completed_documents_since_reset", 0)
    resets: list[bool] = []
    monkeypatch.setattr(
        embedder_tasks, "reset_embed_model", lambda: resets.append(True)
    )

    assert embedder_tasks.maybe_reset_embed_model_after_document() is False
    assert resets == []
    assert embedder_tasks._completed_documents_since_reset == 1

    assert embedder_tasks.maybe_reset_embed_model_after_document() is True
    assert resets == [True]
    assert embedder_tasks._completed_documents_since_reset == 0


def test_maybe_reset_embed_model_after_document_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder_tasks.cfg, "embedding_model_reset_after_documents", 0)
    monkeypatch.setattr(embedder_tasks, "_completed_documents_since_reset", 0)
    monkeypatch.setattr(
        embedder_tasks,
        "reset_embed_model",
        lambda: (_ for _ in ()).throw(RuntimeError("should not reset")),
    )

    assert embedder_tasks.maybe_reset_embed_model_after_document() is False
    assert embedder_tasks._completed_documents_since_reset == 0
