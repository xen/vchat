from __future__ import annotations

from types import SimpleNamespace

from jobs.embedder import tasks


class _Tokenizer:
    def __call__(self, text, add_special_tokens=False, truncation=False):
        _ = add_special_tokens, truncation
        # Simple deterministic tokenizer for tests: split by whitespace.
        return {"input_ids": text.split()}


def test_chunk_text_respects_token_limit_and_overlap() -> None:
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 6
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 2
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 1000
    tasks.get_embed_model = lambda: SimpleNamespace(tokenizer=_Tokenizer())
    text = " ".join(f"w{i}" for i in range(20))
    chunks = tasks.chunk_text_word_window(text)
    assert chunks
    # Every chunk must fit max_tokens
    assert all(len(chunk.text.split()) <= 6 for chunk in chunks)
    # Overlap should produce at least one intersecting boundary
    assert len(chunks) >= 3
    assert chunks[1].start < chunks[0].end


def test_chunk_text_splits_long_token() -> None:
    tasks.EMBEDDING_CHUNK_MAX_TOKENS = 100
    tasks.EMBEDDING_CHUNK_OVERLAP_TOKENS = 0
    tasks.EMBEDDING_CHUNK_MAX_CHARS = 4
    tasks.get_embed_model = lambda: SimpleNamespace(tokenizer=_Tokenizer())
    chunks = tasks.chunk_text_word_window("abcdefghij")
    assert [chunk.text for chunk in chunks] == ["abcd", "efgh", "ij"]
