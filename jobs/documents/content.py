from __future__ import annotations

import hashlib

from vchat.settings import cfg

CHUNK_TEXT_HASH_IGNORED_CHARS = "\u200b\u200c\u200d\ufeff"
_CHUNK_TEXT_HASH_TRANSLATION = str.maketrans("", "", CHUNK_TEXT_HASH_IGNORED_CHARS)


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_chunk_text_for_hash(value: str) -> str:
    return (value or "").translate(_CHUNK_TEXT_HASH_TRANSLATION).strip()


def chunk_text_sha256(value: str) -> str:
    return content_sha256(normalize_chunk_text_for_hash(value))


def is_document_too_big(content: str) -> bool:
    return len(content or "") > cfg.embedding_document_max_chars


def document_too_big_message(content: str) -> str:
    return (
        "Document content is too large to index "
        f"({len(content or '')} chars > {cfg.embedding_document_max_chars})."
    )


def raw_document_too_big_message(size: int, max_size: int) -> str:
    return f"Downloaded file is too large to index ({size} bytes > {max_size} bytes)."
