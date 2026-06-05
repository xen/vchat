from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobs.embedder import chunking as tasks


class _WordTokenizer:
    """Each whitespace-word is one token. Used to test normal chunking logic."""

    def __call__(self, text, add_special_tokens=False, truncation=False, verbose=True):
        _ = add_special_tokens, truncation, verbose
        return {"input_ids": text.split()}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        _ = skip_special_tokens, clean_up_tokenization_spaces
        return " ".join(ids)


class _CharTokenizer:
    """Each character is one token. Used to test long-word splitting."""

    def __call__(self, text, add_special_tokens=False, truncation=False, verbose=True):
        _ = add_special_tokens, truncation, verbose
        return {"input_ids": list(text)}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        _ = skip_special_tokens, clean_up_tokenization_spaces
        return "".join(ids)


def test_chunk_text_respects_token_limit_and_overlap() -> None:
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 6
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 2
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 1000
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()
    text = " ".join(f"w{i}" for i in range(20))
    chunks = tasks.chunk_text_word_window(text)
    assert chunks
    # Every chunk must fit max_tokens
    assert all(len(chunk.text.split()) <= 6 for chunk in chunks)
    # Overlap should produce at least one intersecting boundary
    assert len(chunks) >= 3
    assert chunks[1].start < chunks[0].end


def test_chunk_text_splits_long_token() -> None:
    # EMBEDDING_CHUNK_MAX_CHARS=4 triggers the long-word branch for "abcdefghij".
    # With a char-level tokenizer and max_tokens=4, the 10 token IDs are split
    # into slices of 4 and decoded back to text.
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 4
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 0
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 4
    tasks.get_embed_tokenizer = lambda: _CharTokenizer()
    chunks = tasks.chunk_text_word_window("abcdefghij")
    assert [chunk.text for chunk in chunks] == ["abcd", "efgh", "ij"]


def test_chunk_text_progresses_when_overlap_exceeds_chunk_size() -> None:
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 6
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 400
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 15
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    chunks = tasks.chunk_text_word_window("aaaaa bbbbb ccccc ddddd")

    assert [chunk.text for chunk in chunks] == [
        "aaaaa bbbbb",
        "bbbbb ccccc",
        "ccccc ddddd",
    ]
    assert [chunk.start for chunk in chunks] == [0, 1, 2]


def test_split_text_block_for_chunking_breaks_large_block() -> None:
    chunks = tasks.split_text_block_for_chunking(
        "alpha beta gamma delta epsilon zeta eta theta",
        max_chars=12,
    )
    assert chunks
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_validate_chunk_data_rejects_embedder_oversize() -> None:
    chunk = tasks.ChunkData(
        index=0,
        start=0,
        end=1,
        text="payload",
        kind="text",
        token_count=tasks.EMBEDDING_MAX_SEQ_LENGTH + 1,
    )

    with pytest.raises(tasks.EmbedderDocumentError) as exc:
        tasks.validate_chunk_data([chunk], page_id=77)

    assert exc.value.page_id == 77
    assert "too large for embedder" in str(exc.value)


def test_mark_page_embedder_failed_sets_status_and_cleans_chunks() -> None:
    from jobs.crawler import tasks as crawler_tasks
    from vchat.page_status import PageStatus, PageStatusError

    executed = []
    page = SimpleNamespace(
        id=55,
        source_id=3,
        status=None,
        status_error=None,
        meta={"existing": "value"},
    )

    class _Session:
        def get(self, model, page_id):
            _ = model
            return page if page_id == 55 else None

        def execute(self, stmt):
            executed.append(stmt)
            return None

        def commit(self):
            executed.append("commit")

    crawler_tasks.mark_page_embedder_failed(
        _Session(),
        55,
        message="Chunk exploded",
        error="Chunk exploded",
        exception_class="EmbedderDocumentError",
    )

    assert page.status == PageStatus.parsing
    assert page.status_error == PageStatusError.embedder_failed
    assert page.meta["reason"] == PageStatusError.embedder_failed.value
    assert page.meta["message"] == "Chunk exploded"
    assert page.meta["error"] == "Chunk exploded"
    assert page.meta["exception_class"] == "EmbedderDocumentError"
    assert executed


def test_materialize_page_chunks_rolls_back_after_boilerplate_load(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(
        crawler_tasks, "load_boilerplate_hashes", lambda *_args: frozenset({1})
    )
    monkeypatch.setattr(
        crawler_tasks,
        "chunk_document_text",
        lambda *_args, **_kwargs: [
            tasks.ChunkData(
                index=0,
                start=0,
                end=1,
                text="hello world",
                kind="text",
                token_count=2,
            )
        ],
    )

    calls = []
    page = SimpleNamespace(
        id=77,
        source_id=9,
        content="hello world",
        hash_value="content-hash",
        meta={},
        status=None,
        status_error=None,
    )

    class _Session:
        def __init__(self) -> None:
            self._in_transaction = True

        def in_transaction(self) -> bool:
            return self._in_transaction

        def rollback(self) -> None:
            calls.append("rollback")
            self._in_transaction = False

        def execute(self, stmt):
            calls.append(("execute", stmt))
            return None

        def add(self, obj):
            calls.append(("add", obj))

        def commit(self):
            calls.append("commit")

        def expunge_all(self):
            calls.append("expunge_all")

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert "rollback" not in calls
    assert page.meta[crawler_tasks.INDEX_CONTENT_HASH_META_KEY] == "content-hash"


def test_materialize_page_chunks_marks_oversize_document_too_big(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks
    from vchat.page_status import PageStatus, PageStatusError

    monkeypatch.setattr(crawler_tasks, "EMBEDDING_DOCUMENT_MAX_CHARS", 10)
    monkeypatch.setattr(
        crawler_tasks,
        "document_too_big_message",
        lambda content: (
            f"Document content is too large to index ({len(content or '')} chars > 10)."
        ),
    )
    monkeypatch.setattr(
        crawler_tasks,
        "chunk_document_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("must not chunk oversize document")
        ),
    )

    page = SimpleNamespace(
        id=77,
        source_id=None,
        content="x" * 11,
        hash_value="content-hash",
        meta={},
        status=None,
        status_error=None,
    )
    calls = []

    class _Session:
        def get(self, model, page_id):
            _ = model
            return page if page_id == 77 else None

        def execute(self, stmt):
            calls.append(("execute", stmt))
            return None

        def commit(self):
            calls.append("commit")

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 0
    assert page.status == PageStatus.ready
    assert page.status_error == PageStatusError.too_big
    assert page.meta["reason"] == PageStatusError.too_big.value
    assert page.meta["message"] == (
        "Document content is too large to index (11 chars > 10)."
    )
    assert calls


def test_fetch_page_context_keeps_transaction_open_for_index_lock() -> None:
    from jobs.crawler import tasks as crawler_tasks

    row = SimpleNamespace(
        id=12,
        source_id=3,
        content="payload",
        _hash="content-hash",
        meta={},
        status_error=None,
    )

    class _Result:
        def first(self):
            return row

    class _Session:
        def __init__(self) -> None:
            self.rollbacks = 0
            self._in_transaction = True

        def execute(self, stmt):
            _ = stmt
            return _Result()

        def in_transaction(self) -> bool:
            return self._in_transaction

        def rollback(self) -> None:
            self.rollbacks += 1
            self._in_transaction = False

    session = _Session()
    context = crawler_tasks.fetch_page_context(session, 12)

    assert context is not None
    assert context.id == 12
    assert context.content == "payload"
    assert context.content_hash == "content-hash"
    assert session.rollbacks == 0


def test_fetch_page_context_marks_old_oversize_error_too_big(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks
    from vchat.page_status import PageStatus, PageStatusError

    monkeypatch.setattr(crawler_tasks, "EMBEDDING_DOCUMENT_MAX_CHARS", 10)
    monkeypatch.setattr(
        crawler_tasks,
        "document_too_big_message",
        lambda content: (
            f"Document content is too large to index ({len(content or '')} chars > 10)."
        ),
    )

    row = SimpleNamespace(
        id=12,
        source_id=3,
        content="x" * 11,
        _hash="content-hash",
        meta={},
        status_error=PageStatusError.embedder_failed,
    )
    page = SimpleNamespace(
        id=12,
        source_id=3,
        status=PageStatus.parsing,
        status_error=PageStatusError.embedder_failed,
        meta={},
    )
    calls = []

    class _Result:
        def first(self):
            return row

    class _Session:
        def execute(self, stmt):
            calls.append(("execute", stmt))
            return _Result()

        def get(self, model, page_id):
            _ = model
            return page if page_id == 12 else None

        def commit(self):
            calls.append("commit")

    assert crawler_tasks.fetch_page_context(_Session(), 12) is None
    assert page.status == PageStatus.ready
    assert page.status_error == PageStatusError.too_big
    assert page.meta["reason"] == PageStatusError.too_big.value
    assert page.meta["message"] == (
        "Document content is too large to index (11 chars > 10)."
    )
    assert calls
