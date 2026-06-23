from __future__ import annotations

import pytest

from vchat.llm_cache import (
    cache_candidate_payload,
    record_chat_answer_cache_candidate,
)


class _DB:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)


def test_cache_candidate_payload_uses_strict_rag_conditions() -> None:
    payload = cache_candidate_payload(
        user_text=" Как подать заявку? ",
        used_chunks=[
            {
                "id": 1,
                "document_id": 10,
                "chunk_ix": 0,
                "uri": "https://example.test/page",
            }
        ],
        context_policy={"reason_code": "rag_grounded"},
        request_status="ok",
        guardrail_blocked=False,
        messages_count=1,
    )

    assert payload["cache_candidate"] is True
    assert payload["cache_candidate_reason"] == "strict_exact_candidate"
    assert len(payload["cache_question_hash"]) == 64
    assert len(payload["cache_retrieval_context_hash"]) == 64


@pytest.mark.asyncio
async def test_record_chat_answer_cache_candidate_builds_upsert_statement() -> None:
    db = _DB()

    cache_key = await record_chat_answer_cache_candidate(
        db,
        question_hash="q" * 64,
        retrieval_context_hash="r" * 64,
        provider="openai",
        model="gpt-4o-mini",
        widget_code="public",
        context_policy={"reason_code": "rag_grounded"},
        coverage={"sources_count": 1},
        sources=[{"uri": "https://example.test/page", "title": "Page"}],
        used_chunks=[
            {
                "id": 1,
                "document_id": 10,
                "chunk_ix": 0,
                "uri": "https://example.test/page",
            }
        ],
        answer_text="Ответ",
        tokens=123,
    )

    assert len(cache_key) == 64
    assert len(db.statements) == 1
    statement = db.statements[0]
    params = statement.compile().params
    assert params["purpose"] == "chat_answer"
    assert params["cache_key"] == cache_key
    assert params["question_hash"] == "q" * 64
    assert params["retrieval_context_hash"] == "r" * 64
    assert params["provider"] == "openai"
    assert params["model"] == "gpt-4o-mini"
    assert params["tokens"] == 123
    assert params["key_payload"]["widget_code"] == "public"
    assert params["response_payload"]["answer_text"] == "Ответ"
    assert params["source_scope"] == {
        "widget_code": "public",
        "source_uris": ["https://example.test/page"],
        "page_ids": [10],
        "chunks_count": 1,
        "sources_count": 1,
    }
