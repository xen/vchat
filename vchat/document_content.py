from __future__ import annotations

import hashlib

from vchat.settings import config

INDEXABLE_DOCUMENT_MAX_CHARS = max(
    1,
    int(config.get("embedding_document_max_chars", 1_000_000) or 1_000_000),
)


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_document_too_big(content: str) -> bool:
    return len(content or "") > INDEXABLE_DOCUMENT_MAX_CHARS


def document_too_big_message(content: str) -> str:
    return (
        "Document content is too large to index "
        f"({len(content or '')} chars > {INDEXABLE_DOCUMENT_MAX_CHARS})."
    )
