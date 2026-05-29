from __future__ import annotations

from types import SimpleNamespace

from jobs.embedder import tasks


class _WordTokenizer:
    """Each whitespace-word is one token. Used to test normal chunking logic."""

    def __call__(self, text, add_special_tokens=False, truncation=False):
        _ = add_special_tokens, truncation
        return {"input_ids": text.split()}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        _ = skip_special_tokens, clean_up_tokenization_spaces
        return " ".join(ids)


class _CharTokenizer:
    """Each character is one token. Used to test long-word splitting."""

    def __call__(self, text, add_special_tokens=False, truncation=False):
        _ = add_special_tokens, truncation
        return {"input_ids": list(text)}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        _ = skip_special_tokens, clean_up_tokenization_spaces
        return "".join(ids)


def test_chunk_text_respects_token_limit_and_overlap() -> None:
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 6
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 2
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 1000
    tasks.get_embed_model = lambda: SimpleNamespace(tokenizer=_WordTokenizer())
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
    tasks.get_embed_model = lambda: SimpleNamespace(tokenizer=_CharTokenizer())
    chunks = tasks.chunk_text_word_window("abcdefghij")
    assert [chunk.text for chunk in chunks] == ["abcd", "efgh", "ij"]
