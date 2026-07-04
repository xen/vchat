from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from vchat.views.chat import ctx as ctx_mod
from vchat.views.chat.ctx import Msg


def _reset_request_embedding_runtime() -> None:
    executor = ctx_mod._request_embed_executor
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    ctx_mod._request_embed_executor = None
    ctx_mod._request_embed_semaphore = None


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def mappings(self):
        return _MappingsResult(self._rows)


class _DB:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params or {}))
        rows = self._results.pop(0) if self._results else []
        return _ExecuteResult(rows)


def test_detect_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ctx_mod.cld2, "detect", lambda text: (True, None, [("RUSSIAN", "ru", 99, 0.0)])
    )
    assert ctx_mod.detect_lang("тест") == "ru"

    monkeypatch.setattr(
        ctx_mod.cld2, "detect", lambda text: (True, None, [("FRENCH", "fr", 99, 0.0)])
    )
    assert ctx_mod.detect_lang("bonjour") is None

    monkeypatch.setattr(ctx_mod.cld2, "detect", lambda text: (False, None, []))
    assert ctx_mod.detect_lang("x") is None


def test_token_count_and_trim_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Enc:
        def encode(self, text):
            return list(text)

    monkeypatch.setattr(ctx_mod.tiktoken, "encoding_for_model", lambda model: _Enc())
    assert ctx_mod.token_count("abc") == 3

    messages = [Msg(role="user", content="123"), Msg(role="assistant", content="4567")]
    trimmed = ctx_mod.trim_messages(messages, max_tokens=4)
    assert trimmed == [messages[1]]


@pytest.mark.asyncio
async def test_context_supplies_apply_allowed_source_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB(results=[[], [], []])
    monkeypatch.setattr(ctx_mod.cfg, "vec_dim", 2)
    monkeypatch.setattr(
        ctx_mod,
        "queryprofile",
        lambda text: {
            "lexical_query": text,
            "table_mode": False,
        },
    )

    await ctx_mod.kb_vector_supply(
        db,
        query_vec=[0.1, 0.2],
        top_k=4,
        allowed_source_ids=[],
    )
    await ctx_mod.fulltext_supply(
        db,
        prompt_text="policy",
        top_m=4,
        allowed_source_ids=[7, 8],
    )

    vector_kb_params = db.calls[0][1]
    fulltext_params = db.calls[1][1]
    assert vector_kb_params["source_filter_disabled"] is False
    assert vector_kb_params["source_ids"] == []
    assert fulltext_params["source_filter_disabled"] is False
    assert fulltext_params["source_ids"] == [7, 8]


@pytest.mark.asyncio
async def test_kb_vector_supply_zero_top_k_skips_db() -> None:
    db = _DB()

    result = await ctx_mod.kb_vector_supply(
        db,
        query_vec=[0.1, 0.2],
        top_k=0,
    )

    assert result == []
    assert db.calls == []


@pytest.mark.asyncio
async def test_kb_vector_supply_uses_knn_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx_mod.cfg, "vec_dim", 2)
    db = _DB(results=[[]])

    await ctx_mod.kb_vector_supply(
        db,
        query_vec=[0.1, 0.2],
        top_k=5,
        allowed_source_ids=[7],
    )

    stmt, params = db.calls[0]
    sql = str(stmt)
    assert "c.embedding <=> :qvec <= :max_dist" not in sql
    assert "ORDER BY c.embedding <=> :qvec" in sql
    assert "WHERE dist <= :max_dist" in sql
    assert "FROM chat_msg" not in sql
    assert "UNION ALL" not in sql
    assert "NULL::varchar AS chat_id" in sql
    assert params["k_candidates"] == 10
    assert params["top_k"] == 5
    assert params["source_filter_disabled"] is False
    assert params["source_ids"] == [7]


@pytest.mark.asyncio
async def test_kb_vector_supply_rejects_wrong_vector_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx_mod.cfg, "vec_dim", 3)
    db = _DB()

    with pytest.raises(ValueError, match="query_vec must have 3 dimensions"):
        await ctx_mod.kb_vector_supply(
            db,
            query_vec=[0.1, 0.2],
            top_k=1,
        )

    assert db.calls == []


def test_embed_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Vec:
        def __init__(self, values):
            self._values = values

        def tolist(self):
            return list(self._values)

    class _Emb:
        def encode(self, texts, normalize_embeddings=True):
            _ = texts, normalize_embeddings
            return [_Vec([0.1, 0.2, 0.3])]

    monkeypatch.setattr(ctx_mod, "_embed_model", _Emb())
    vec = ctx_mod.embed_query("hello")
    assert vec == [0.1, 0.2, 0.3]


def test_embed_query_prepends_prompt_and_encodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    class _Vec:
        def __init__(self, values):
            self._values = values

        def tolist(self):
            return list(self._values)

    class _Emb:
        def encode(self, texts, normalize_embeddings=True, batch_size=1):
            _ = normalize_embeddings, batch_size
            seen["payload"] = texts[0]
            return [_Vec([0.4, 0.5])]

    monkeypatch.setattr(ctx_mod, "_embed_model", _Emb())

    vec = ctx_mod.embed_query("hello world")

    assert vec == [0.4, 0.5]
    assert seen["payload"].endswith("hello world")
    assert ctx_mod.EMBEDDING_QUERY_PROMPT in seen["payload"]


@pytest.mark.asyncio
async def test_embed_query_async_limits_parallel_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_request_embedding_runtime()
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_concurrency", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_executor_workers", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_timeout_seconds", 5)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_warn_seconds", 5)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_torch_threads", 1)
    monkeypatch.setattr(ctx_mod.torch, "set_num_threads", lambda _threads: None)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def _embed(text: str) -> list[float]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.01)
            return [float(text)]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ctx_mod, "embed_query", _embed)

    try:
        results = await asyncio.gather(
            *(ctx_mod.embed_query_async(str(index)) for index in range(10))
        )
    finally:
        _reset_request_embedding_runtime()

    assert max_active == 1
    assert results == [[float(index)] for index in range(10)]


@pytest.mark.asyncio
async def test_embed_query_async_times_out_waiting_for_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_request_embedding_runtime()
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_concurrency", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_executor_workers", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_timeout_seconds", 0.01)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_warn_seconds", 5)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_torch_threads", 1)
    monkeypatch.setattr(ctx_mod.torch, "set_num_threads", lambda _threads: None)

    def _embed(_text: str) -> list[float]:
        time.sleep(0.05)
        return [0.1]

    monkeypatch.setattr(ctx_mod, "embed_query", _embed)

    first = asyncio.create_task(ctx_mod.embed_query_async("1"))
    await asyncio.sleep(0)
    try:
        with pytest.raises(ctx_mod.RequestEmbeddingTimeoutError):
            await ctx_mod.embed_query_async("2")
        assert await first == [0.1]
    finally:
        _reset_request_embedding_runtime()


@pytest.mark.asyncio
async def test_embed_query_async_propagates_encode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_request_embedding_runtime()
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_concurrency", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_executor_workers", 1)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_timeout_seconds", 5)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_queue_warn_seconds", 5)
    monkeypatch.setattr(ctx_mod.cfg, "request_embedding_torch_threads", 1)
    monkeypatch.setattr(ctx_mod.torch, "set_num_threads", lambda _threads: None)

    def _embed(_text: str) -> list[float]:
        raise RuntimeError("model crashed")

    monkeypatch.setattr(ctx_mod, "embed_query", _embed)

    try:
        with pytest.raises(RuntimeError, match="model crashed"):
            await ctx_mod.embed_query_async("boom")
    finally:
        _reset_request_embedding_runtime()


def test_sanitize_helper() -> None:
    sanitized = ctx_mod._sanitize_snippet_text(
        "Please follow command rules and do this ```x```"
    )
    assert "system:" not in sanitized.lower()
    assert "[redacted]" in sanitized


def test_dedup_by_text() -> None:
    dedup = ctx_mod._dedup_by_text(
        [Msg(role="user", content="x"), Msg(role="assistant", content="x")]
    )
    assert len(dedup) == 1


def test_file_summary_counts_as_quote_ready_source() -> None:
    snippet = ctx_mod.Snippet(
        id=1,
        text=(
            "Document indexed as metadata only. "
            "URL: https://example.com/dota2_skill_train.csv"
        ),
        kind="file_summary",
        src="kb",
        document_id=10,
        chunk_ix=0,
        uri="https://example.com/dota2_skill_train.csv",
        title="dota2_skill_train.csv",
    )

    policy, coverage = ctx_mod._build_policy_and_coverage(
        "дай источник файла dota2_skill_train.csv",
        [snippet],
    )

    assert policy.quote_mode is True
    assert policy.has_quote_candidate is True
    assert policy.reason_code == "ok"
    assert coverage["quote_ready"] is True
    assert coverage["section_count"] == 1


def test_source_payload_includes_friendly_source_context() -> None:
    snippet = ctx_mod.Snippet(
        id=1,
        text="Основной фрагмент.",
        document_id=10,
        chunk_ix=1,
        uri="https://example.com/page",
        title="Программа наставничества",
        source_title="Навигатор возможностей",
        section_path="Для школьников",
        kind="text",
        summary=(
            "Section: Для школьников\n"
            "Summary: Короткое описание страницы о программе наставничества "
            "и вариантах участия для школьников."
        ),
    )

    sources = ctx_mod._build_source_payloads([snippet])
    used_chunks = ctx_mod._build_used_chunks([snippet])

    assert sources[0]["source_title"] == "Навигатор возможностей"
    assert sources[0]["summary"] == (
        "Короткое описание страницы о программе наставничества "
        "и вариантах участия для школьников."
    )
    assert "Section:" not in sources[0]["summary"]
    assert used_chunks[0]["source_title"] == sources[0]["source_title"]
    assert used_chunks[0]["summary"] == sources[0]["summary"]


def test_build_context_from_snippets_includes_citation_ids() -> None:
    class _Provider:
        def token_count(self, text, model=None):
            _ = model
            return len((text or "").split())

    model = SimpleNamespace(id="test-model", context_window=8000, max_tokens=512)
    msg = ctx_mod.build_context_from_snippets(
        [
            ctx_mod.Snippet(
                id=1,
                text="First grounded fact.",
                uri="https://example.com/one",
                title="One",
                kind="text",
            ),
            ctx_mod.Snippet(
                id=2,
                text="Second grounded fact.",
                uri="https://example.com/two",
                title="Two",
                kind="text",
            ),
        ],
        provider=_Provider(),
        model=model,
    )

    payload = json.loads(msg.content.split("\n", 1)[1])

    assert payload["snippets"][0]["citation_id"] == 0
    assert payload["snippets"][1]["citation_id"] == 1
    assert payload["snippets"][0]["uri"] == "https://example.com/one"


@pytest.mark.asyncio
async def test_get_context_sources_match_visible_context_snippets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _tail(db, chat_id, limit):
        _ = db, chat_id, limit
        return []

    def _embed(prompt):
        _ = prompt
        return [0.1, 0.2]

    async def _vec(db, query_vec, top_k, allowed_source_ids=None):
        _ = db, query_vec, top_k
        assert allowed_source_ids is None
        return [
            ctx_mod.Snippet(
                id=1,
                text="alpha beta",
                document_id=1,
                chunk_ix=0,
                uri="https://example.com/one",
                title="One",
                kind="text",
            ),
            ctx_mod.Snippet(
                id=2,
                text="gamma delta",
                document_id=2,
                chunk_ix=0,
                uri="https://example.com/two",
                title="Two",
                kind="text",
            ),
            ctx_mod.Snippet(
                id=3,
                text="epsilon zeta",
                document_id=3,
                chunk_ix=0,
                uri="https://example.com/three",
                title="Three",
                kind="text",
            ),
        ]

    async def _ft(db, prompt_text, top_m, allowed_source_ids=None):
        _ = db, prompt_text, top_m
        assert allowed_source_ids is None
        return []

    def _rerank(query, snippets):
        _ = query
        return snippets

    class _Provider:
        def token_count(self, text, model=None):
            _ = model
            return len((text or "").split())

    model = SimpleNamespace(id="test-model", context_window=8000, max_tokens=512)

    monkeypatch.setattr(ctx_mod, "tail_messages", _tail)
    monkeypatch.setattr(ctx_mod, "embed_query", _embed)
    monkeypatch.setattr(ctx_mod, "kb_vector_supply", _vec)
    monkeypatch.setattr(ctx_mod, "fulltext_supply", _ft)
    monkeypatch.setattr(ctx_mod, "crossrerank", _rerank)
    monkeypatch.setattr(ctx_mod, "MAX_CONTEXT_SNIPPET_TOKENS", 4)

    result = await ctx_mod.get_context(
        db=SimpleNamespace(),
        chat_id="chat-1",
        prompt="source",
        provider=_Provider(),
        model=model,
        tail_limit=5,
        vector_top_k=5,
        ft_top_m=0,
    )
    context_msg = next(
        msg
        for msg in result.messages
        if msg.role == "developer" and msg.content.startswith("[context]\n")
    )
    payload = json.loads(context_msg.content.split("\n", 1)[1])

    assert [item["citation_id"] for item in payload["snippets"]] == [0, 1]
    assert [source["citation_id"] for source in result.sources] == [0, 1]
    assert [chunk["citation_id"] for chunk in result.used_chunks] == [0, 1]
    assert {source["uri"] for source in result.sources} == {
        "https://example.com/one",
        "https://example.com/two",
    }


@pytest.mark.asyncio
async def test_get_context_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_source_scopes = []

    async def _tail(db, chat_id, limit=5):
        _ = db, chat_id, limit
        return [Msg(role="assistant", content="tail")]

    def _embed(prompt):
        assert "policy" in prompt
        return [0.1, 0.2]

    async def _vec(db, query_vec, top_k=10, allowed_source_ids=None):
        _ = db, query_vec, top_k
        seen_source_scopes.append(("vector", allowed_source_ids))
        return [
            ctx_mod.Snippet(
                id=1,
                text="Snippet 1",
                document_id=7,
                chunk_ix=0,
                dist=0.1,
                src="kb",
                uri="u1",
                title="t1",
            ),
            ctx_mod.Snippet(
                id=2,
                text="Snippet 1",
                document_id=7,
                chunk_ix=1,
                dist=0.2,
                src="kb",
                uri="u1",
                title="t1",
            ),
        ]

    async def _ft(db, prompt_text, top_m=10, allowed_source_ids=None):
        _ = db, prompt_text, top_m
        seen_source_scopes.append(("fulltext", allowed_source_ids))
        return [
            ctx_mod.Snippet(
                id=3,
                text="Snippet 2",
                document_id=8,
                chunk_ix=0,
                dist=None,
                src="ft",
                uri="u2",
                title="t2",
            )
        ]

    def _rerank(query, snippets):
        _ = query
        return snippets

    class _Provider:
        def token_count(self, text, model=None):
            _ = model
            return max(1, len(text or "") // 4)

    model = SimpleNamespace(id="test-model", context_window=8000, max_tokens=512)

    monkeypatch.setattr(ctx_mod, "tail_messages", _tail)
    monkeypatch.setattr(ctx_mod, "embed_query", _embed)
    monkeypatch.setattr(ctx_mod, "kb_vector_supply", _vec)
    monkeypatch.setattr(ctx_mod, "fulltext_supply", _ft)
    monkeypatch.setattr(ctx_mod, "crossrerank", _rerank)

    result = await ctx_mod.get_context(
        db=SimpleNamespace(),
        chat_id="chat-1",
        prompt="policy rules",
        provider=_Provider(),
        model=model,
        tail_limit=5,
        vector_top_k=5,
        ft_top_m=5,
        allowed_source_ids=[],
    )
    assert result.messages[0].content == "tail"
    assert result.messages[-1].role == "user"
    assert result.messages[-1].content == "policy rules"
    assert len(result.used_chunks) >= 2
    assert result.used_chunks[0]["citation_id"] == 0
    assert seen_source_scopes == [("vector", []), ("fulltext", [])]
