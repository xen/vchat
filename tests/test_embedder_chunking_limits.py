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


@pytest.fixture(autouse=True)
def _disable_chunk_dedup_for_fake_sessions(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(
        crawler_tasks,
        "mark_duplicate_page_chunks",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        crawler_tasks,
        "reuse_existing_chunk_embeddings",
        lambda *_args, **_kwargs: 0,
    )


def _set_chunk_cfg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_tokens: int,
    overlap_tokens: int,
    max_chars: int,
) -> None:
    monkeypatch.setattr(tasks.cfg, "embedding_chunk_max_tokens", max_tokens)
    monkeypatch.setattr(tasks.cfg, "embedding_chunk_overlap_tokens", overlap_tokens)
    monkeypatch.setattr(tasks.cfg, "embedding_chunk_max_chars", max_chars)


def _patch_meta(page, *, remove=(), **updates) -> None:
    meta = dict(page.meta or {})
    for key in remove:
        meta.pop(key, None)
    meta.update(updates)
    page.meta = meta


def test_chunk_text_respects_token_limit_and_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=6, overlap_tokens=2, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()
    text = " ".join(f"w{i}" for i in range(20))
    chunks = tasks.chunk_text_word_window(text)
    assert chunks
    # Every chunk must fit max_tokens
    assert all(len(chunk.text.split()) <= 6 for chunk in chunks)
    # Overlap should produce at least one intersecting boundary
    assert len(chunks) >= 3
    assert chunks[1].start < chunks[0].end


def test_chunk_text_splits_long_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # embedding_chunk_max_chars=4 triggers the long-word branch for "abcdefghij".
    # With a char-level tokenizer and max_tokens=4, the 10 token IDs are split
    # into slices of 4 and decoded back to text.
    _set_chunk_cfg(monkeypatch, max_tokens=4, overlap_tokens=0, max_chars=4)
    tasks.get_embed_tokenizer = lambda: _CharTokenizer()
    chunks = tasks.chunk_text_word_window("abcdefghij")
    assert [chunk.text for chunk in chunks] == ["abcd", "efgh", "ij"]


def test_chunk_text_caps_overlap_when_overlap_exceeds_chunk_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=6, overlap_tokens=400, max_chars=15)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    chunks = tasks.chunk_text_word_window("aaaaa bbbbb ccccc ddddd")

    assert [chunk.text for chunk in chunks] == [
        "aaaaa bbbbb",
        "ccccc ddddd",
    ]
    assert [chunk.start for chunk in chunks] == [0, 2]


def test_chunk_text_uses_token_overlap_not_word_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=40, overlap_tokens=30, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _CharTokenizer()

    text = " ".join("abcdefghij" for _ in range(80))
    chunks = tasks.chunk_text_word_window(text)

    assert chunks
    assert all(chunk.token_count <= 40 for chunk in chunks)
    assert len(chunks) <= 30
    assert sum(len(chunk.text) for chunk in chunks) < len(text) * 2


def test_split_text_block_for_chunking_breaks_large_block() -> None:
    chunks = tasks.split_text_block_for_chunking(
        "alpha beta gamma delta epsilon zeta eta theta",
        max_chars=12,
    )
    assert chunks
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_chunk_document_normalizes_full_html_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=20, overlap_tokens=4, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Internal title</title>
        <style>.secret { color: red; }</style>
        <script>window.secretToken = "must not index";</script>
      </head>
      <body>
        <nav>Main menu that should not be indexed</nav>
        <main>
          <h1>Useful page</h1>
          <p>Grounded answer text remains visible.</p>
        </main>
      </body>
    </html>
    """

    chunks = tasks.chunk_document_text(html)
    joined = "\n".join(chunk.text for chunk in chunks)

    assert "Useful page" in joined
    assert "Grounded answer text remains visible." in joined
    assert "must not index" not in joined
    assert "Main menu that should not be indexed" not in joined
    assert "<html" not in joined.lower()


def test_chunk_document_drops_html_ui_config_json_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=20, overlap_tokens=4, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    html = """
    <!DOCTYPE html>
    <html>
      <body>
        {"isActive":false,"cookieKey":null}
        <main>
          <h1>Documents</h1>
          <p>Application materials remain searchable.</p>
        </main>
      </body>
    </html>
    """

    chunks = tasks.chunk_document_text(html)
    joined = "\n".join(chunk.text for chunk in chunks)

    assert "Documents" in joined
    assert "Application materials remain searchable." in joined
    assert "cookieKey" not in joined
    assert "isActive" not in joined


def test_chunk_document_keeps_plain_text_json_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=20, overlap_tokens=4, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    chunks = tasks.chunk_document_text(
        '{"isActive": false, "cookieKey": "documented option"}'
    )

    assert any("documented option" in chunk.text for chunk in chunks)


def test_chunk_document_does_not_treat_inline_html_mention_as_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chunk_cfg(monkeypatch, max_tokens=20, overlap_tokens=4, max_chars=1000)
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    chunks = tasks.chunk_document_text("Use the <html> tag in examples.")

    assert any("<html>" in chunk.text for chunk in chunks)


def test_validate_chunk_data_rejects_embedder_oversize() -> None:
    chunk = tasks.ChunkData(
        index=0,
        start=0,
        end=1,
        text="payload",
        kind="text",
        token_count=tasks.cfg.embedding_max_seq_length + 1,
    )

    with pytest.raises(tasks.EmbedderDocumentError) as exc:
        tasks.validate_chunk_data([chunk], page_id=77)

    assert exc.value.page_id == 77
    assert "too large for embedder" in str(exc.value)


def test_mark_page_embedder_failed_sets_status_and_cleans_chunks() -> None:
    from jobs.crawler import tasks as crawler_tasks
    from vchat.views.projects.page_status import PageStatus, PageStatusError

    executed = []
    page = SimpleNamespace(
        id=55,
        source_id=3,
        status=None,
        status_error=None,
        meta={"existing": "value"},
    )
    page.patch_meta = lambda **kwargs: _patch_meta(page, **kwargs)

    class _Session:
        def get(self, model, page_id):
            _ = model
            return page if page_id == 55 else None

        def execute(self, stmt):
            executed.append(stmt)
            return None

        def flush(self):
            pass

        def commit(self):
            executed.append("commit")

    crawler_tasks.mark_page_embedder_failed(
        _Session(),
        55,
        crawler_tasks.EmbedderDocumentError("Chunk exploded"),
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

        def flush(self):
            pass

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
    from vchat.views.projects.page_status import PageStatus, PageStatusError

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 10)
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
    page.patch_meta = lambda **kwargs: _patch_meta(page, **kwargs)
    calls = []

    class _Session:
        def get(self, model, page_id):
            _ = model
            return page if page_id == 77 else None

        def execute(self, stmt):
            calls.append(("execute", stmt))
            return None

        def flush(self):
            pass

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


def test_materialize_page_chunks_sizes_full_html_by_visible_text(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 30)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    page = SimpleNamespace(
        id=78,
        source_id=None,
        uri="https://example.com/documents",
        title="Documents",
        content="""
        <!DOCTYPE html>
        <html>
          <body>
            <script>window.largeState = "%s";</script>
            <nav>%s</nav>
            <main>Useful visible text.</main>
          </body>
        </html>
        """
        % ("x" * 500, "menu " * 200),
        hash_value="content-hash",
        meta={"doc_type": "html", "content_type": "text/html"},
        status=None,
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=1500,
    )
    added = []
    calls = []

    class _Session:
        def execute(self, stmt):
            calls.append(("execute", stmt))
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            calls.append("commit")

        def expunge_all(self):
            calls.append("expunge_all")

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 2
    assert page.status_error is None
    text_chunks = [chunk for chunk in added if chunk.kind == "text"]
    assert len(text_chunks) == 1
    assert text_chunks[0].text == "Useful visible text."
    assert page.meta[crawler_tasks.INDEX_CONTENT_HASH_META_KEY] == "content-hash"


def test_materialize_page_chunks_uses_metadata_only_for_vendor_asset(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    page = SimpleNamespace(
        id=88,
        source_id=None,
        uri="https://example.com/assets/vendor/codemirror/CHANGELOG/",
        title="CodeMirror changelog",
        content="Fix focus tracking in shadow DOM.\n" * 200,
        hash_value="content-hash",
        meta={"doc_type": "html", "content_type": "text/html"},
        status=None,
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=6400,
    )
    added = []
    calls = []

    class _Session:
        def execute(self, stmt):
            calls.append(("execute", stmt))
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            calls.append("commit")

        def expunge_all(self):
            calls.append("expunge_all")

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert page.meta["index_policy"] == "metadata_only"
    assert page.meta["index_policy_reason"] == "vendor_asset"
    assert len(added) == 1
    assert added[0].kind == "file_summary"
    assert "CodeMirror changelog" in added[0].text
    assert "https://example.com/assets/vendor/codemirror/CHANGELOG/" in added[0].text


def test_materialize_page_chunks_uses_metadata_only_for_giant_csv(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 10)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    page = SimpleNamespace(
        id=89,
        source_id=None,
        uri="https://example.com/upload/csv/dota2_skill_train.csv",
        title="dota2_skill_train.csv",
        content="hero_id,match_id,win\n1,2,1\n",
        hash_value="content-hash",
        meta={"doc_type": "code", "content_type": "text/csv"},
        status=None,
        status_error=None,
        raw_content_type="text/csv",
        raw_content_size=22_000_000,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert page.meta["index_policy"] == "metadata_only"
    assert page.meta["index_policy_reason"] == "csv_statistical_dump"
    assert added[0].kind == "file_summary"
    assert "hero_id,match_id,win" in added[0].text


def test_materialize_page_chunks_detects_large_statistical_dump_without_csv_hint(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 10_000)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    rows = ["user_id|match_id|score|duration|rank|won"]
    rows.extend(
        f"{idx}|{idx + 1000}|{idx % 50}|{idx * 3}|{idx % 10}|{idx % 2}"
        for idx in range(120)
    )
    content = "\n".join(rows)
    page = SimpleNamespace(
        id=94,
        source_id=None,
        uri="https://example.com/data/export",
        title="match export",
        content=content,
        hash_value="content-hash",
        meta={"doc_type": "text", "content_type": "text/plain"},
        status=None,
        status_error=None,
        raw_content_type="text/plain",
        raw_content_size=2_000_000,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert page.meta["index_policy"] == "metadata_only"
    assert page.meta["index_policy_reason"] == "csv_statistical_dump"
    assert added[0].kind == "file_summary"
    assert "user_id|match_id|score" in added[0].text


def test_materialize_page_chunks_keeps_article_with_few_delimited_lines_full_text(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 10_000)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    delimited_examples = "\n".join(
        f"field_a,field_b,field_c,{idx}" for idx in range(12)
    )
    content = (
        "This article explains how to import small CSV examples safely.\n"
        f"{delimited_examples}\n"
        "The grounded answer is in the prose, not in a statistical dump."
    )
    page = SimpleNamespace(
        id=95,
        source_id=None,
        uri="https://example.com/articles/csv-import",
        title="CSV import article",
        content=content,
        hash_value="content-hash",
        meta={"doc_type": "html", "content_type": "text/html"},
        status=None,
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=len(content),
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count > 0
    assert page.meta.get("index_policy") is None
    assert any(chunk.kind == "text" for chunk in added)
    assert any("grounded answer" in chunk.text for chunk in added)


def test_materialize_page_chunks_uses_metadata_only_for_large_downloadable_document(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 80)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    page = SimpleNamespace(
        id=92,
        source_id=None,
        uri="https://example.com/library/program-guide.pdf",
        title="Program guide",
        content="Extracted PDF body about deadlines and requirements. " * 20,
        hash_value="content-hash",
        meta={"doc_type": "pdf", "content_type": "application/pdf"},
        status=None,
        status_error=None,
        raw_content_type="application/pdf",
        raw_content_size=3_000_000,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert page.meta["index_policy"] == "metadata_only"
    assert page.meta["index_policy_reason"] == "large_downloadable_document"
    assert added[0].kind == "file_summary"
    assert "Program guide" in added[0].text
    assert "application/pdf" in added[0].text
    assert "deadlines and requirements" in added[0].text


def test_materialize_page_chunks_keeps_large_raw_downloadable_document_full_text(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 1000)
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())
    tasks.get_embed_tokenizer = lambda: _WordTokenizer()

    page = SimpleNamespace(
        id=93,
        source_id=None,
        uri="https://example.com/library/short-guide.pdf",
        title="Short guide",
        content="Short extracted PDF text with a grounded answer.",
        hash_value="content-hash",
        meta={
            "doc_type": "pdf",
            "content_type": "application/pdf",
            "index_policy": "metadata_only",
            "index_policy_reason": "large_downloadable_document",
        },
        status=None,
        status_error=None,
        raw_content_type="application/pdf",
        raw_content_size=3_000_000,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count > 0
    assert page.meta.get("index_policy") is None
    assert any(chunk.kind == "text" for chunk in added)
    assert any("grounded answer" in chunk.text for chunk in added)


def test_materialize_page_chunks_uses_metadata_only_for_empty_visible_html(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    page = SimpleNamespace(
        id=90,
        source_id=None,
        uri="https://teacher.vbudushee.ru/",
        title="Школа возможностей",
        content="""
        <!DOCTYPE html>
        <html>
          <body>
            <script>window.app = {"title": "Школа возможностей"};</script>
            <nav>Menu</nav>
          </body>
        </html>
        """,
        hash_value="content-hash",
        meta={"doc_type": "html", "content_type": "text/html"},
        status=None,
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=400,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 1
    assert page.meta["index_policy"] == "metadata_only"
    assert page.meta["index_policy_reason"] == "empty_visible_text"
    assert added[0].kind == "file_summary"
    assert "Школа возможностей" in added[0].text
    assert "window.app" not in added[0].text


def test_materialize_page_chunks_skips_empty_visible_redirect_page(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    page = SimpleNamespace(
        id=91,
        source_id=None,
        uri="https://lpconference.vbudushee.ru/",
        title="301 Moved Permanently",
        content="# 301 Moved Permanently",
        hash_value="content-hash",
        meta={"doc_type": "html", "content_type": "text/html"},
        status=None,
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=23,
    )
    added = []

    class _Session:
        def execute(self, stmt):
            _ = stmt
            return None

        def add(self, obj):
            added.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def expunge_all(self):
            pass

    count = crawler_tasks.materialize_page_chunks(_Session(), page)

    assert count == 0
    assert added == []
    assert "index_policy" not in page.meta


def test_fetch_page_context_keeps_transaction_open_for_index_lock() -> None:
    from jobs.crawler import tasks as crawler_tasks

    row = SimpleNamespace(
        id=12,
        source_id=3,
        uri="https://example.com/doc",
        title="Doc",
        content="payload",
        _hash="content-hash",
        meta={},
        status_error=None,
        raw_content_type="text/html",
        raw_content_size=7,
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
    from vchat.views.projects.page_status import PageStatus, PageStatusError

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 10)
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
        uri="https://example.com/doc",
        title="Doc",
        content="x" * 11,
        _hash="content-hash",
        meta={},
        status_error=PageStatusError.embedder_failed,
        raw_content_type="text/html",
        raw_content_size=11,
    )
    page = SimpleNamespace(
        id=12,
        source_id=3,
        status=PageStatus.parsing,
        status_error=PageStatusError.embedder_failed,
        meta={},
    )
    page.patch_meta = lambda **kwargs: _patch_meta(page, **kwargs)
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

        def flush(self):
            pass

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


def test_fetch_page_context_allows_old_too_big_when_visible_html_fits(
    monkeypatch,
) -> None:
    from jobs.crawler import tasks as crawler_tasks
    from vchat.views.projects.page_status import PageStatusError

    monkeypatch.setattr(crawler_tasks.cfg, "embedding_document_max_chars", 30)

    row = SimpleNamespace(
        id=13,
        source_id=3,
        uri="https://example.com/documents",
        title="Documents",
        content="""
        <!DOCTYPE html>
        <html>
          <body>
            <script>window.largeState = "%s";</script>
            <main>Useful visible text.</main>
          </body>
        </html>
        """
        % ("x" * 500),
        _hash="content-hash",
        meta={},
        status_error=PageStatusError.too_big,
        raw_content_type="text/html",
        raw_content_size=700,
    )
    calls = []

    class _Result:
        def first(self):
            return row

    class _Session:
        def execute(self, stmt):
            calls.append(("execute", stmt))
            return _Result()

    context = crawler_tasks.fetch_page_context(_Session(), 13)

    assert context is not None
    assert context.id == 13
    assert context.status_error == PageStatusError.too_big
    assert calls
