import hashlib
import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vchat.models import LLMCacheEntry

CHAT_ANSWER_CACHE_PURPOSE = "chat_answer"
LLM_CACHE_KEY_VERSION = 1


def stable_cache_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_cache_question(text: str) -> str:
    return " ".join(text.casefold().split())


def retrieval_context_hash(used_chunks: list[dict[str, Any]]) -> str | None:
    if not used_chunks:
        return None
    payload = [
        {
            "chunk_id": chunk.get("id"),
            "page_id": chunk.get("document_id") or chunk.get("page_id"),
            "chunk_ix": chunk.get("chunk_ix"),
            "text_hash": chunk.get("text_hash"),
            "uri": chunk.get("uri"),
        }
        for chunk in used_chunks
    ]
    return stable_cache_hash(payload)


def cache_candidate_payload(
    *,
    user_text: str,
    used_chunks: list[dict[str, Any]],
    context_policy: dict[str, Any],
    request_status: str,
    guardrail_blocked: bool,
    messages_count: int,
) -> dict[str, Any]:
    normalized_question = normalize_cache_question(user_text)
    retrieval_hash = retrieval_context_hash(used_chunks)
    reason_code = str(context_policy.get("reason_code") or "")
    eligible = (
        request_status == "ok"
        and not guardrail_blocked
        and messages_count <= 1
        and bool(retrieval_hash)
        and reason_code not in {"no_context", "missing_source_url"}
    )
    return {
        "cache_candidate": eligible,
        "cache_candidate_reason": "strict_exact_candidate" if eligible else "no_cache",
        "cache_question_hash": stable_cache_hash(normalized_question),
        "cache_retrieval_context_hash": retrieval_hash,
    }


def build_chat_answer_cache_key_payload(
    *,
    question_hash: str,
    retrieval_context_hash: str,
    provider: str | None,
    model: str | None,
    widget_code: str | None,
    context_policy: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": LLM_CACHE_KEY_VERSION,
        "purpose": CHAT_ANSWER_CACHE_PURPOSE,
        "question_hash": question_hash,
        "retrieval_context_hash": retrieval_context_hash,
        "provider": provider,
        "model": model,
        "widget_code": widget_code,
        "context_reason_code": context_policy.get("reason_code"),
    }


def source_scope_payload(
    *,
    used_chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    widget_code: str | None,
) -> dict[str, Any]:
    source_uris = sorted(
        {
            str(source.get("uri") or "").strip()
            for source in sources
            if str(source.get("uri") or "").strip()
        }
    )
    page_ids = sorted(
        {
            int(page_id)
            for chunk in used_chunks
            for page_id in [chunk.get("document_id") or chunk.get("page_id")]
            if isinstance(page_id, int)
        }
    )
    return {
        "widget_code": widget_code,
        "source_uris": source_uris,
        "page_ids": page_ids,
        "chunks_count": len(used_chunks),
        "sources_count": len(sources),
    }


async def record_chat_answer_cache_candidate(
    db: AsyncSession,
    *,
    question_hash: str,
    retrieval_context_hash: str,
    provider: str | None,
    model: str | None,
    widget_code: str | None,
    context_policy: dict[str, Any],
    coverage: dict[str, Any],
    sources: list[dict[str, Any]],
    used_chunks: list[dict[str, Any]],
    answer_text: str,
    tokens: int,
) -> str:
    key_payload = build_chat_answer_cache_key_payload(
        question_hash=question_hash,
        retrieval_context_hash=retrieval_context_hash,
        provider=provider,
        model=model,
        widget_code=widget_code,
        context_policy=context_policy,
    )
    cache_key = stable_cache_hash(key_payload)
    response_payload = {
        "answer_text": answer_text,
        "answer_chars": len(answer_text),
        "sources": sources,
        "coverage": coverage,
        "context_policy": context_policy,
    }
    source_scope = source_scope_payload(
        used_chunks=used_chunks,
        sources=sources,
        widget_code=widget_code,
    )
    stmt = pg_insert(LLMCacheEntry).values(
        purpose=CHAT_ANSWER_CACHE_PURPOSE,
        cache_key=cache_key,
        question_hash=question_hash,
        retrieval_context_hash=retrieval_context_hash,
        key_payload=key_payload,
        response_payload=response_payload,
        source_scope=source_scope,
        provider=provider,
        model=model,
        tokens=max(int(tokens or 0), 0),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_llm_cache_entry_purpose_key",
        set_={
            "response_payload": stmt.excluded.response_payload,
            "source_scope": stmt.excluded.source_scope,
            "provider": stmt.excluded.provider,
            "model": stmt.excluded.model,
            "tokens": stmt.excluded.tokens,
            "observed_count": LLMCacheEntry.observed_count + 1,
            "potential_saved_requests": LLMCacheEntry.potential_saved_requests + 1,
            "potential_saved_tokens": (
                LLMCacheEntry.potential_saved_tokens + stmt.excluded.tokens
            ),
            "last_seen_at": sa.func.now(),
            "updated_at": sa.func.now(),
        },
    )
    await db.execute(stmt)
    return cache_key
