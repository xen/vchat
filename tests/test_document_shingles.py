"""Tests for the word-trigram boilerplate detection pipeline.

Covers:
- extract_content_blocks: markdown → block list
- compute_trigram_hashes: block → frozenset[int]
- is_boilerplate_block: block × boilerplate_set → bool
- chunk_document_text: boilerplate_hashes param filters blocks
- rebuild_boilerplate_for_source: counts shingles across pages
"""

from __future__ import annotations

from types import SimpleNamespace

from vchat.document_shingles import (
    compute_trigram_hashes,
    extract_content_blocks,
    is_boilerplate_block,
    normalize_words,
)


# ---------------------------------------------------------------------------
# normalize_words
# ---------------------------------------------------------------------------


def test_normalize_words_lowercases_and_keeps_alphanum() -> None:
    assert normalize_words("Михаил Кашкин, 2024!") == ["михаил", "кашкин", "2024"]


def test_normalize_words_empty() -> None:
    assert normalize_words("") == []


# ---------------------------------------------------------------------------
# compute_trigram_hashes
# ---------------------------------------------------------------------------


def test_compute_trigram_hashes_requires_at_least_3_words() -> None:
    assert compute_trigram_hashes("one two") == frozenset()
    assert compute_trigram_hashes("one") == frozenset()
    assert compute_trigram_hashes("") == frozenset()


def test_compute_trigram_hashes_produces_hashes_for_3_words() -> None:
    h = compute_trigram_hashes("alpha beta gamma")
    assert len(h) == 1  # exactly one trigram
    assert all(isinstance(v, int) for v in h)


def test_compute_trigram_hashes_signed_64bit() -> None:
    # All hashes must fit in signed 64-bit range (PostgreSQL BIGINT)
    text = "a b c d e f g h i j"
    hashes = compute_trigram_hashes(text)
    assert all(-(2**63) <= h < 2**63 for h in hashes)


def test_compute_trigram_hashes_identical_texts_produce_same_set() -> None:
    t = "встречаем гостей у входа в здание"
    assert compute_trigram_hashes(t) == compute_trigram_hashes(t)


def test_compute_trigram_hashes_different_texts_produce_different_sets() -> None:
    a = compute_trigram_hashes("кошка сидит на коврике у двери")
    b = compute_trigram_hashes("собака бежит по парку в лесу")
    # Disjoint trigrams → disjoint hash sets
    assert a.isdisjoint(b)


def test_compute_trigram_hashes_count_equals_n_minus_2() -> None:
    words = "a b c d e f g"
    h = compute_trigram_hashes(words)
    # 7 words → 5 trigrams (may collapse if hashes collide, but very unlikely)
    assert len(h) == 5


# ---------------------------------------------------------------------------
# extract_content_blocks
# ---------------------------------------------------------------------------


def test_extract_content_blocks_splits_on_headers() -> None:
    text = (
        "вводный абзац про сайт\n"
        "## Section One\n"
        "контент первого раздела сайта\n"
        "## Section Two\n"
        "контент второго раздела страницы"
    )
    blocks = extract_content_blocks(text)
    assert len(blocks) == 3
    assert "вводный" in blocks[0]
    assert "первого раздела" in blocks[1]
    assert "второго раздела" in blocks[2]


def test_extract_content_blocks_drops_short_blocks() -> None:
    text = "ok\n## H\na b"  # "ok" has 1 word, "a b" has 2 words — both dropped
    blocks = extract_content_blocks(text)
    assert blocks == []


def test_extract_content_blocks_keeps_blocks_with_3_or_more_words() -> None:
    text = "## H\nalpha beta gamma"
    blocks = extract_content_blocks(text)
    assert len(blocks) == 1
    assert "alpha beta gamma" in blocks[0]


def test_extract_content_blocks_handles_empty_text() -> None:
    assert extract_content_blocks("") == []


def test_extract_content_blocks_no_headers() -> None:
    text = "paragraph one two three\nanother line four"
    blocks = extract_content_blocks(text)
    assert len(blocks) == 1  # single block, no headers


# ---------------------------------------------------------------------------
# is_boilerplate_block
# ---------------------------------------------------------------------------

NAV_TEXT = "навигация главная страница контакты о нас услуги цены блог"


def test_is_boilerplate_block_false_when_no_boilerplate_hashes() -> None:
    assert is_boilerplate_block(NAV_TEXT, frozenset()) is False


def test_is_boilerplate_block_true_when_all_hashes_are_boilerplate() -> None:
    hashes = compute_trigram_hashes(NAV_TEXT)
    assert is_boilerplate_block(NAV_TEXT, hashes) is True


def test_is_boilerplate_block_false_for_unique_content() -> None:
    nav_hashes = compute_trigram_hashes(NAV_TEXT)
    content = (
        "михаил кашкин работал в яндексе с две тысячи первого по две тысячи десятый"
    )
    assert is_boilerplate_block(content, nav_hashes) is False


def test_is_boilerplate_block_false_for_short_block() -> None:
    # Block with < 3 trigrams → never flagged boilerplate
    hashes = frozenset(range(1000))  # huge boilerplate set
    assert is_boilerplate_block("раз два три", hashes) is False  # only 1 trigram


def test_is_boilerplate_block_threshold_50_percent() -> None:
    # Build a block whose first half is boilerplate and second half is unique.
    boilerplate_part = "навигация главная страница контакты о нас"
    unique_part = "уникальный текст специфичный для этой страницы автора"
    block = f"{boilerplate_part} {unique_part}"
    boilerplate_hashes = compute_trigram_hashes(boilerplate_part)
    block_hashes = compute_trigram_hashes(block)
    overlap = len(block_hashes & boilerplate_hashes)
    # Overlap is less than 50% → not boilerplate
    if overlap / len(block_hashes) < 0.5:
        assert is_boilerplate_block(block, boilerplate_hashes) is False


# ---------------------------------------------------------------------------
# chunk_document_text: boilerplate_hashes filters blocks
# ---------------------------------------------------------------------------


class _WordTokenizer:
    """Whitespace-word tokenizer for tests (no model required)."""

    def __call__(self, text, add_special_tokens=False, truncation=False):
        return {"input_ids": text.split()}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        return " ".join(ids)


def make_fake_model():
    return SimpleNamespace(tokenizer=_WordTokenizer())


def test_chunk_document_text_skips_boilerplate_blocks(monkeypatch) -> None:
    """Blocks whose trigrams are fully in boilerplate_hashes must be excluded."""
    from jobs.embedder import tasks

    monkeypatch.setattr(tasks, "get_embed_model", make_fake_model)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_MAX_TOKENS", 200)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_OVERLAP_TOKENS", 0)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_MAX_CHARS", 10000)

    boilerplate_block = "навигация главная страница контакты о нас услуги"
    content_block = "михаил кашкин консультирует стартапы и обучает программированию"
    text = f"## Nav\n{boilerplate_block}\n## Content\n{content_block}"

    boilerplate_hashes = compute_trigram_hashes(boilerplate_block)

    # Without filter: both blocks produce chunks
    chunks_unfiltered = tasks.chunk_document_text(text)
    texts_unfiltered = [c.text for c in chunks_unfiltered]
    assert any(boilerplate_block in t for t in texts_unfiltered), (
        "Without filter, boilerplate block should appear in chunks"
    )

    # With filter: boilerplate block must be absent
    chunks_filtered = tasks.chunk_document_text(
        text, boilerplate_hashes=boilerplate_hashes
    )
    texts_filtered = [c.text for c in chunks_filtered]
    assert not any(boilerplate_block in t for t in texts_filtered), (
        "With boilerplate_hashes, boilerplate block must be excluded"
    )
    assert any(content_block in t for t in texts_filtered), (
        "Content block must still appear after boilerplate filtering"
    )


def test_chunk_document_text_no_filter_when_hashes_empty(monkeypatch) -> None:
    """Empty boilerplate_hashes → no blocks dropped."""
    from jobs.embedder import tasks

    monkeypatch.setattr(tasks, "get_embed_model", make_fake_model)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_MAX_TOKENS", 200)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_OVERLAP_TOKENS", 0)
    monkeypatch.setattr(tasks, "EMBEDDING_CHUNK_MAX_CHARS", 10000)

    text = "## A\nнавигация главная страница контакты о нас\n## B\nуникальный контент страницы"
    chunks_no_filter = tasks.chunk_document_text(text, boilerplate_hashes=None)
    chunks_empty_filter = tasks.chunk_document_text(
        text, boilerplate_hashes=frozenset()
    )
    assert len(chunks_no_filter) == len(chunks_empty_filter)


# ---------------------------------------------------------------------------
# rebuild_boilerplate_for_source
# ---------------------------------------------------------------------------


def test_rebuild_boilerplate_for_source_counts_shingles() -> None:
    """rebuild_boilerplate_for_source counts how many pages share each trigram."""
    from jobs.embedder.tasks import rebuild_boilerplate_for_source

    inserted_batches: list[list[dict[str, int]]] = []
    deleted_source_id: list[int] = []
    rollbacks = []

    class FakeResult:
        def scalars(self):
            return self

        def __iter__(self):
            # Two pages: one shared block + one unique block each
            nav = "навигация главная страница контакты о нас услуги"
            return iter(
                [
                f"{nav}\nУникальное содержание первой страницы автора блога",
                f"{nav}\nДругое уникальное содержание второй страницы сайта",
                ]
            )

    class FakeSession:
        def __init__(self) -> None:
            self._in_transaction = True

        def execute(self, stmt, params=None):
            # Detect if it's a select or delete
            if hasattr(stmt, "is_delete") and stmt.is_delete:
                return None
            if hasattr(stmt, "is_insert") and stmt.is_insert:
                inserted_batches.append(list(params or []))
                return None
            return FakeResult()

        def rollback(self):
            rollbacks.append(True)
            self._in_transaction = False

        def in_transaction(self):
            return self._in_transaction

        def commit(self):
            pass

    session = FakeSession()

    # Patch sa.delete to track deletions
    def fake_delete(table):
        class Stmt:
            is_delete = True

            def where(self, *args):
                if hasattr(args[0], "left"):
                    try:
                        deleted_source_id.append(int(str(args[0].right.value)))
                    except Exception:
                        pass
                return self

        return Stmt()

    def fake_insert(table):
        class Stmt:
            is_insert = True

        return Stmt()

    import jobs.embedder.tasks as tasks_module

    original_sa_delete = tasks_module.sa.delete
    original_sa_insert = tasks_module.sa.insert
    tasks_module.sa.delete = fake_delete
    tasks_module.sa.insert = fake_insert
    try:
        count = rebuild_boilerplate_for_source(session, source_id=42)
    finally:
        tasks_module.sa.delete = original_sa_delete
        tasks_module.sa.insert = original_sa_insert

    added = [item for batch in inserted_batches for item in batch]

    # Some shingles from the navigation block appear in both pages → count = 2
    nav_hashes = compute_trigram_hashes(
        "навигация главная страница контакты о нас услуги"
    )
    nav_in_added = [row for row in added if row["shingle_hash"] in nav_hashes]
    assert any(row["count"] == 2 for row in nav_in_added), (
        "Navigation trigrams must have count=2 (appear in both pages)"
    )

    # Total distinct shingles must match what was added
    assert count == len(added)
    assert rollbacks == [True]
