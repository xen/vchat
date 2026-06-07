from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from vchat.views.chat import ctx as ctx_mod
from vchat.views.chat import views as chat_views


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "rag_quality"
    / "answer_grounding_cases.json"
)
REQUIRED_CASE_TYPES = {
    "exact_fact_lookup",
    "faq_help_answer",
    "procedural_instruction_answer",
    "table_numeric_lookup",
    "quote_source_request",
    "broad_page_summary",
    "multi_section_enumeration",
    "negative_query_absent",
    "noisy_source_context",
    "downloadable_document_query",
}
CITATION_RE = re.compile(r"\[\[citation:(\d+)]]")
NEGATIVE_MARKERS = (
    "could not find",
    "not found",
    "not mentioned",
    "no mention",
    "не найден",
    "не нашел",
    "нет в источниках",
    "в источниках нет",
)


class EvalProvider:
    def token_count(self, text: str, model: Any = None) -> int:
        _ = model
        return len((text or "").split())


def load_cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text())


def normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def cited_source_urls(case: dict[str, Any], answer: str | None = None) -> set[str]:
    sources_by_id = {
        int(source["citation_id"]): source for source in case.get("sources", [])
    }
    cited_ids = {
        int(match.group(1)) for match in CITATION_RE.finditer(answer or case["answer"])
    }
    unknown_ids = cited_ids - sources_by_id.keys()
    assert not unknown_ids, f"Unknown citation ids: {sorted(unknown_ids)}"
    return {sources_by_id[citation_id]["uri"] for citation_id in cited_ids}


def assert_grounded_answer(case: dict[str, Any], answer: str) -> None:
    normalized_answer = normalized(answer)
    cited_urls = cited_source_urls(case, answer=answer)

    if case["citation_required"]:
        assert cited_urls, "Expected at least one citation"
        assert set(case["expected_source_urls"]) <= cited_urls

    for expected_fact in case["expected_answer_facts"]:
        assert normalized(expected_fact) in normalized_answer

    for forbidden_claim in case["forbidden_claims"]:
        assert normalized(forbidden_claim) not in normalized_answer

    forbidden_citation_urls = set(case["forbidden_citation_urls"])
    assert not forbidden_citation_urls.intersection(cited_urls)

    cited_source_kinds = {
        source["kind"] for source in case["sources"] if source["uri"] in cited_urls
    }
    forbidden_source_kinds = set(case["forbidden_source_kinds"])
    assert not forbidden_source_kinds.intersection(cited_source_kinds)

    if case.get("negative_answer_required"):
        assert any(marker in normalized_answer for marker in NEGATIVE_MARKERS)
        assert not cited_urls, "Absent-answer cases should not cite irrelevant sources"


def assert_case_schema(case: dict[str, Any]) -> None:
    required_keys = {
        "name",
        "case_type",
        "user_query",
        "answer",
        "sources",
        "expected_source_urls",
        "expected_source_titles",
        "expected_answer_facts",
        "forbidden_claims",
        "forbidden_citation_urls",
        "forbidden_source_kinds",
        "citation_required",
        "acceptable_answer_notes",
        "current_baseline_result",
    }
    missing_keys = required_keys - case.keys()
    assert not missing_keys, f"{case['name']} missing keys: {sorted(missing_keys)}"
    for source in case["sources"]:
        assert source.get("text"), f"{case['name']} source missing text"


def source_snippets(case: dict[str, Any]) -> list[ctx_mod.Snippet]:
    return [
        ctx_mod.Snippet(
            id=source["citation_id"],
            text=source["text"],
            document_id=source["citation_id"],
            chunk_ix=0,
            uri=source["uri"],
            title=source["title"],
            kind=source["kind"],
        )
        for source in case["sources"]
    ]


def context_message(case: dict[str, Any]) -> str:
    model = type("EvalModel", (), {"id": "eval-model"})()
    return ctx_mod.build_context_from_snippets(
        source_snippets(case),
        provider=EvalProvider(),
        model=model,
    ).content


def context_payload(case: dict[str, Any]) -> dict[str, Any]:
    return json.loads(context_message(case).split("\n", 1)[1])


def generation_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": chat_views.SYSTEM_PROMPT},
        {"role": "developer", "content": context_message(case)},
        {"role": "user", "content": case["user_query"]},
    ]


def context_and_user_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    return generation_messages(case)[1:]
