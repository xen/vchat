from __future__ import annotations

import hashlib

from vchat.settings import config

CHUNK_TEXT_HASH_IGNORED_CHARS = "\u200b\u200c\u200d\ufeff"
_CHUNK_TEXT_HASH_TRANSLATION = str.maketrans("", "", CHUNK_TEXT_HASH_IGNORED_CHARS)

INDEXABLE_DOCUMENT_MAX_CHARS = max(
    1,
    int(config.get("embedding_document_max_chars", 1_000_000) or 1_000_000),
)


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_chunk_text_for_hash(value: str) -> str:
    return (value or "").translate(_CHUNK_TEXT_HASH_TRANSLATION).strip()


def chunk_text_sha256(value: str) -> str:
    return content_sha256(normalize_chunk_text_for_hash(value))


def is_document_too_big(content: str) -> bool:
    return len(content or "") > INDEXABLE_DOCUMENT_MAX_CHARS


def document_too_big_message(content: str) -> str:
    return (
        "Document content is too large to index "
        f"({len(content or '')} chars > {INDEXABLE_DOCUMENT_MAX_CHARS})."
    )
