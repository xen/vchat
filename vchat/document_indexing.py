from __future__ import annotations

import hashlib
import re
from typing import Any

import sqlalchemy as sa

from vchat.document_shingles import extract_shingles
from vchat.models import Chunk, Document

NEAR_DUPLICATE_SHINGLE_SIZE = 3
NEAR_DUPLICATE_SIMILARITY_THRESHOLD = 0.9


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def document_content_unchanged(document: Document | None, content: str) -> bool:
    return document is not None and document.hash_value == content_sha256(content)


def _normalized_lines(text: str) -> list[str]:
    normalized: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue
        line = re.sub(r"\b\d{1,4}(?:[./:-]\d{1,4})+\b", "<date>", line)
        line = re.sub(r"\b\d+\b", "<num>", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            normalized.append(line)
    return normalized


def content_shingle_set(text: str, k: int = NEAR_DUPLICATE_SHINGLE_SIZE) -> set[str]:
    normalized_lines = _normalized_lines(text)
    normalized_text = "\n".join(normalized_lines)
    shingles = extract_shingles(normalized_text, k=k)
    if shingles:
        return set(shingles)
    return set(normalized_lines)


def shingle_jaccard_similarity(
    left: str,
    right: str,
    *,
    k: int = NEAR_DUPLICATE_SHINGLE_SIZE,
) -> float:
    left_shingles = content_shingle_set(left, k=k)
    right_shingles = content_shingle_set(right, k=k)
    if not left_shingles and not right_shingles:
        return 1.0
    if not left_shingles or not right_shingles:
        return 0.0
    intersection = len(left_shingles & right_shingles)
    union = len(left_shingles | right_shingles)
    if union == 0:
        return 1.0
    return intersection / union


def document_content_effectively_unchanged(
    document: Document | None,
    content: str,
    *,
    similarity_threshold: float = NEAR_DUPLICATE_SIMILARITY_THRESHOLD,
    k: int = NEAR_DUPLICATE_SHINGLE_SIZE,
) -> bool:
    if document is None:
        return False
    if document_content_unchanged(document, content):
        return True
    previous_content = (document.content or "").strip()
    current_content = (content or "").strip()
    if not previous_content or not current_content:
        return False
    similarity = shingle_jaccard_similarity(previous_content, current_content, k=k)
    return similarity >= similarity_threshold


def sync_document_has_chunks(session: Any, document_id: int) -> bool:
    return (
        session.execute(
            sa.select(Chunk.id).where(Chunk.document_id == document_id).limit(1)
        ).first()
        is not None
    )


async def async_document_has_chunks(session: Any, document_id: int) -> bool:
    return (
        (
            await session.execute(
                sa.select(Chunk.id).where(Chunk.document_id == document_id).limit(1)
            )
        ).first()
        is not None
    )
