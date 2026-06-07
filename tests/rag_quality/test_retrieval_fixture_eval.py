from __future__ import annotations

import re
from dataclasses import asdict

import pytest

from tests.rag_quality import answer_eval
from vchat.views.chat import ctx as ctx_mod


class _UniformReranker:
    def predict(self, pairs, show_progress_bar=False):
        _ = show_progress_bar
        return [0.2 for _ in pairs]


def _query_terms(case: dict) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    payload = " ".join([case["user_query"], *case["expected_answer_facts"]])
    if case.get("negative_answer_required"):
        payload = " ".join([case["user_query"], *case["forbidden_claims"]])
    for term in re.findall(r"[\w.$@/-]+", payload.casefold()):
        normalized = term.strip(".,:;!?()[]{}\"'")
        if (
            len(normalized) < 3
            or normalized in seen
            or normalized in ctx_mod.LEXICAL_STOP_TERMS
        ):
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms[:8]


def _distractors(case: dict) -> list[ctx_mod.Snippet]:
    return [
        ctx_mod.Snippet(
            id=10_000,
            text="General product overview and unrelated company announcement.",
            document_id=10_000,
            chunk_ix=0,
            uri="https://noise.example.com/overview",
            title="Noise overview",
            kind="text",
            src="fixture_noise",
        ),
        ctx_mod.Snippet(
            id=10_001,
            text="Summary: release background, team notes, and broad navigation text.",
            document_id=10_001,
            chunk_ix=0,
            uri="https://noise.example.com/summary",
            title="Noise summary",
            kind="summary",
            src="fixture_noise",
        ),
        ctx_mod.Snippet(
            id=10_002,
            text=f"Unrelated search page mentioning only: {case['user_query']}",
            document_id=10_002,
            chunk_ix=0,
            uri="https://noise.example.com/search",
            title="Search noise",
            kind="section_summary",
            src="fixture_noise",
        ),
    ]


@pytest.mark.parametrize("case", answer_eval.load_cases(), ids=lambda item: item["name"])
def test_fixture_retrieval_source_precision(
    case: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer_eval.assert_case_schema(case)
    monkeypatch.setattr(ctx_mod, "_rerank_model", _UniformReranker())
    monkeypatch.setattr(
        ctx_mod,
        "queryprofile",
        lambda query: {
            "lexical_query": query,
            "lexical_terms": _query_terms(case),
            "table_mode": case["case_type"] == "table_numeric_lookup",
            "quote_mode": case["case_type"] == "quote_source_request",
            "enumeration_mode": case["case_type"] == "multi_section_enumeration",
        },
    )

    source_snippets = [
        ctx_mod.Snippet(**{**asdict(snippet), "src": "fixture_expected"})
        for snippet in answer_eval.source_snippets(case)
    ]
    candidates = [*_distractors(case), *source_snippets]

    ranked = ctx_mod.crossrerank(case["user_query"], candidates)
    filtered = ctx_mod.filter_snippets_by_document_relevance(ranked)
    selected = ctx_mod.select_context_snippets(
        filtered,
        provider=answer_eval.EvalProvider(),
        model=type("EvalModel", (), {"id": "eval-model"})(),
    )
    selected_urls = [snippet.uri for snippet in selected]

    assert selected_urls[0] in set(case["expected_source_urls"])
    assert set(case["expected_source_urls"]).intersection(selected_urls)
    assert "https://noise.example.com/search" not in selected_urls[:1]


def test_fixture_retrieval_noisy_case_keeps_noise_below_relevant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(
        item
        for item in answer_eval.load_cases()
        if item["name"] == "noisy_context_uses_relevant_source_only"
    )

    test_fixture_retrieval_source_precision(case, monkeypatch)
