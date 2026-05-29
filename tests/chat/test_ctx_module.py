from __future__ import annotations

from types import SimpleNamespace

import pytest

from vchat.views.chat import ctx as ctx_mod
from vchat.views.chat._types import Msg


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

    async def execute(self, stmt, params=None):
        _ = stmt, params
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


def test_text_summarizer_fallback_for_unknown_lang() -> None:
    text = "a" * 250
    out = ctx_mod.text_summarizer(text, 0.5, lang="de")
    assert out.endswith(" …")
    assert len(out) <= 203


def test_text_summarizer_with_fake_nlp(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Sent:
        def __init__(self, text):
            self.text = text
            self._words = [SimpleNamespace(text=w) for w in text.split()]

        def __iter__(self):
            return iter(self._words)

    class _Doc:
        def __init__(self):
            self._tokens = [
                SimpleNamespace(text=t) for t in "alpha beta alpha gamma".split()
            ]
            self.sents = [_Sent("alpha beta"), _Sent("gamma")]

        def __iter__(self):
            return iter(self._tokens)

    monkeypatch.setitem(ctx_mod.lang_models, "en", "fake")
    monkeypatch.setitem(ctx_mod.nlps, "en", lambda text: _Doc())
    out = ctx_mod.text_summarizer("any", 0.5, lang="en")
    assert isinstance(out, str)


def test_token_count_and_trim_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Enc:
        def encode(self, text):
            return list(text)

    monkeypatch.setattr(ctx_mod.tiktoken, "encoding_for_model", lambda model: _Enc())
    assert ctx_mod.token_count("abc") == 3

    messages = [Msg(role="user", content="123"), Msg(role="assistant", content="4567")]
    trimmed = ctx_mod.trim_messages(messages, max_tokens=4)
    assert trimmed == [messages[1]]


def test_embed_query_and_vec_literal(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert ctx_mod._vec_literal([0.1234567, 1.0]) == "[0.123457,1.000000]"


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
async def test_fetch_user_memory_chunks_primary_and_fallback() -> None:
    rows = [
        {"chat_id": "c2", "dist": 0.10, "content": "a"},
        {"chat_id": "c2", "dist": 0.20, "content": "dup"},
        {"chat_id": "c3", "dist": 0.22, "content": "b"},
    ]
    db = _DB(results=[rows])
    out = await ctx_mod._fetch_user_memory_chunks(
        db, user_id=1, chat_id="c1", qvec=[0.1], k_mem=2, tau=0.8
    )
    assert len(out) == 1
    assert {r["chat_id"] for r in out} == {"c2"}

    db2 = _DB(results=[[{"chat_id": "c9", "dist": 0.19, "content": "fallback"}]])
    out2 = await ctx_mod._fetch_user_memory_chunks(
        db2, user_id=1, chat_id="c1", qvec=[0.1], k_mem=1, tau=0.95, tau_fallback=0.8
    )
    assert len(out2) == 1


@pytest.mark.asyncio
async def test_fetch_tail_messages_and_vector_chunks() -> None:
    db = _DB(results=[[("u1", "user"), ("a1", "assistant")]])
    tail = await ctx_mod._fetch_tail_messages(db, "chat-1", limit=2)
    assert [m.role for m in tail] == ["assistant", "user"]

    chat_rows = [{"id": 1, "dist": 0.4, "src": "chat"}]
    kb_rows = [{"id": 2, "dist": 0.2, "src": "kb"}]
    db2 = _DB(results=[chat_rows, kb_rows])
    vec_rows = await ctx_mod._fetch_vector_chunks(db2, "chat-1", [0.1, 0.2], top_k=2)
    assert [r["id"] for r in vec_rows] == [2, 1]


@pytest.mark.asyncio
async def test_fetch_ft_chunks() -> None:
    db = _DB(results=[[{"id": 1, "content": "x"}]])
    rows = await ctx_mod._fetch_ft_chunks(db, "search", top_m=3)
    assert rows and rows[0]["id"] == 1
    assert await ctx_mod._fetch_ft_chunks(db, "", top_m=3) == []
    assert await ctx_mod._fetch_ft_chunks(db, "x", top_m=0) == []


def test_dedup_and_sanitize_helpers() -> None:
    snips = [
        {"content": "line1"},
        {"content": "line1 more"},
        {"content": "line2"},
    ]
    deduped = ctx_mod._dedup_snippets(snips, max_prefix=5)
    assert len(deduped) == 2

    sanitized = ctx_mod._sanitize_snippet_text(
        "Please follow command rules and do this ```x```"
    )
    assert "system:" not in sanitized.lower()
    assert "[redacted]" in sanitized


def test_context_builders() -> None:
    msg = ctx_mod._build_context_from_snippets([{"content": "A"}, {"content": "B"}])
    assert msg.role == "developer"
    assert "[[citation:0]]" in msg.content

    empty = ctx_mod._build_context_from_snippets([])
    assert "нет релевантных" in empty.content

    dedup = ctx_mod._dedup_by_text(
        [Msg(role="user", content="x"), Msg(role="assistant", content="x")]
    )
    assert len(dedup) == 1

    combined = ctx_mod._build_context_message(
        [Msg(role="user", content="u")],
        [Msg(role="assistant", content="a")],
        [Msg(role="assistant", content="a")],
    )
    assert combined[-1].role == "system"
    assert combined[-1].content == "[context]"


@pytest.mark.asyncio
async def test_get_context_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _tail(db, chat_id, limit=5):
        _ = db, chat_id, limit
        return [Msg(role="assistant", content="tail")]

    def _embed(prompt):
        assert "policy" in prompt
        return [0.1, 0.2]

    async def _vec(db, chat_id, query_vec, top_k=10):
        _ = db, chat_id, query_vec, top_k
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

    async def _ft(db, prompt_text, top_m=10):
        _ = db, prompt_text, top_m
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
    monkeypatch.setattr(ctx_mod, "vector_supply", _vec)
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
    )
    assert result.messages[0].content == "tail"
    assert result.messages[-1].role == "user"
    assert result.messages[-1].content == "policy rules"
    assert len(result.used_chunks) >= 2
    assert result.used_chunks[0]["citation_id"] == 0
