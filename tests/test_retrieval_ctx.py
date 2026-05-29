"""
Tests and quality benchmarks for the retrieval pipeline.

Coverage:
  retrieval_config.py   — constant definitions and weight sanity
  reciprocal_rank_fusion — RRF scores, deduplication, origin tracking
  crossrerank            — boosting logic, penalties, content dedup
  System constants       — VECTOR_MAX_DIST, RERANK_LIMIT
  Quality scenarios      — ranking correctness under controlled mock scores
"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

import pytest

import vchat.views.chat.ctx as ctx_mod
from vchat.views.chat.ctx import (
    RERANK_LIMIT,
    RRF_K,
    VECTOR_MAX_DIST,
    Snippet,
    crossrerank,
    reciprocal_rank_fusion,
)
from vchat.views.chat import retrieval_config as rcnf


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_SNIPPET_COUNTER = 0


def make_snippet(
    *,
    text: str = "default text",
    kind: str = "text",
    src: str = "kb",
    document_id: int | None = 1,
    chunk_ix: int | None = 0,
    uri: str | None = "https://example.com/doc",
    title: str | None = "Test doc",
    header_text: str | None = None,
    section_path: str | None = None,
    entity_terms: list[str] | None = None,
    dist: float | None = None,
    chat_id: str | None = None,
    id: int | None = None,
) -> Snippet:
    global _SNIPPET_COUNTER
    _SNIPPET_COUNTER += 1
    return Snippet(
        id=id if id is not None else _SNIPPET_COUNTER,
        text=text,
        kind=kind,
        src=src,
        document_id=document_id,
        chunk_ix=chunk_ix,
        uri=uri,
        title=title,
        header_text=header_text,
        section_path=section_path,
        entity_terms=entity_terms,
        dist=dist,
        chat_id=chat_id,
    )


class _Reranker:
    """Mock cross-encoder that returns a per-snippet controlled score."""

    def __init__(self, scores: list[float] | float = 0.5):
        self._scores = scores

    def predict(self, pairs: list, show_progress_bar: bool = False) -> list[float]:
        if isinstance(self._scores, float):
            return [self._scores] * len(pairs)
        return list(self._scores)


def _patch_reranker(
    monkeypatch: pytest.MonkeyPatch, scores: list[float] | float = 0.5
) -> None:
    monkeypatch.setattr(ctx_mod, "_rerank_model", _Reranker(scores))


def _patch_profile(
    monkeypatch: pytest.MonkeyPatch, terms: list[str], **flags: Any
) -> dict:
    profile = {
        "lexical_query": " OR ".join(terms),
        "lexical_terms": terms,
        "table_mode": flags.get("table_mode", False),
        "quote_mode": flags.get("quote_mode", False),
        "enumeration_mode": flags.get("enumeration_mode", False),
    }
    monkeypatch.setattr(ctx_mod, "queryprofile", lambda _q: profile)
    return profile


# ─────────────────────────────────────────────────────────────────────────────
# 1. retrieval_config — constant sanity
# ─────────────────────────────────────────────────────────────────────────────


class TestRetrievalConfig:
    def test_field_weights_all_positive(self) -> None:
        for field, weight in rcnf.RERANK_FIELD_WEIGHTS.items():
            assert weight > 0, f"RERANK_FIELD_WEIGHTS[{field!r}] must be positive"

    def test_overlap_weight_positive(self) -> None:
        assert rcnf.RERANK_OVERLAP_WEIGHT > 0

    def test_kind_bonus_text_positive(self) -> None:
        assert rcnf.RERANK_KIND_BONUS["text"] > 0

    def test_kind_bonus_summary_positive(self) -> None:
        assert rcnf.RERANK_KIND_BONUS["section_summary"] > 0
        assert rcnf.RERANK_KIND_BONUS["summary"] > 0

    def test_table_mode_bonus_positive(self) -> None:
        for kind, bonus in rcnf.RERANK_TABLE_MODE_BONUS.items():
            assert bonus > 0, f"RERANK_TABLE_MODE_BONUS[{kind!r}] must be positive"

    def test_zero_overlap_penalty_positive(self) -> None:
        # It's a penalty so the constant itself is stored as a positive value
        assert rcnf.RERANK_SUMMARY_ZERO_OVERLAP_PENALTY > 0

    def test_header_text_weight_gte_entity_terms(self) -> None:
        # header_text is more discriminative than entity_terms
        assert (
            rcnf.RERANK_FIELD_WEIGHTS["header_text"]
            >= rcnf.RERANK_FIELD_WEIGHTS["entity_terms"]
        )

    def test_section_path_between_header_and_entity(self) -> None:
        assert (
            rcnf.RERANK_FIELD_WEIGHTS["header_text"]
            >= rcnf.RERANK_FIELD_WEIGHTS["section_path"]
            >= rcnf.RERANK_FIELD_WEIGHTS["entity_terms"]
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. System constants
# ─────────────────────────────────────────────────────────────────────────────


class TestSystemConstants:
    def test_vector_max_dist_is_068(self) -> None:
        assert VECTOR_MAX_DIST == pytest.approx(0.68), (
            "VECTOR_MAX_DIST должен быть 0.68 — исправление порога "
            "vector-recall для фильтрации нерелевантных результатов"
        )

    def test_rerank_limit_is_48(self) -> None:
        assert RERANK_LIMIT == 48, (
            "RERANK_LIMIT должен быть 48 — расширенный пул кандидатов для cross-encoder"
        )

    def test_rrf_k_unchanged(self) -> None:
        assert RRF_K == 60


# ─────────────────────────────────────────────────────────────────────────────
# 3. reciprocal_rank_fusion
# ─────────────────────────────────────────────────────────────────────────────


class TestReciprocal_rank_fusion:
    def test_score_formula_single_ranking(self) -> None:
        """Score for rank-1 item in one list must equal 1/(RRF_K+1)."""
        s = make_snippet(text="unique text alpha")
        result = reciprocal_rank_fusion([[s]])
        assert len(result) == 1
        expected = round(1.0 / (RRF_K + 1), 6)
        assert result[0].rerank_score == pytest.approx(expected)

    def test_score_increases_with_multiple_rankings(self) -> None:
        """Same snippet appearing in two lists gets higher score than once."""
        s = make_snippet(text="shared text beta")
        once = reciprocal_rank_fusion([[s]])
        twice = reciprocal_rank_fusion([[s], [s]])
        assert twice[0].rerank_score > once[0].rerank_score

    def test_dedup_by_key_content(self) -> None:
        """Two identical snippets in different lists count as one.

        The RRF key includes (document_id, chat_id, id, chunk_ix, …, text[:200])
        so both snippets must share the same id to be treated as the same chunk.
        """
        s1 = make_snippet(text="same content gamma", document_id=10, chunk_ix=0, id=999)
        s2 = make_snippet(text="same content gamma", document_id=10, chunk_ix=0, id=999)
        result = reciprocal_rank_fusion([[s1], [s2]])
        assert len(result) == 1

    def test_distinct_snippets_kept_separately(self) -> None:
        a = make_snippet(text="text A delta", document_id=20, chunk_ix=0)
        b = make_snippet(text="text B epsilon", document_id=20, chunk_ix=1)
        result = reciprocal_rank_fusion([[a, b]])
        assert len(result) == 2

    def test_empty_text_skipped(self) -> None:
        empty = make_snippet(text="")
        good = make_snippet(text="non-empty text zeta")
        result = reciprocal_rank_fusion([[empty, good]])
        assert len(result) == 1
        assert result[0].text == "non-empty text zeta"

    def test_whitespace_only_skipped(self) -> None:
        blank = make_snippet(text="   \n  ")
        good = make_snippet(text="real content eta")
        result = reciprocal_rank_fusion([[blank, good]])
        assert len(result) == 1

    def test_higher_rank_gives_lower_score(self) -> None:
        """First item in ranking should beat last."""
        a = make_snippet(text="first item theta")
        b = make_snippet(text="second item iota")
        result = reciprocal_rank_fusion([[a, b]])
        scores = {r.text: r.rerank_score for r in result}
        assert scores["first item theta"] > scores["second item iota"]

    def test_retrieval_origins_single_source(self) -> None:
        """Origin is recorded from the snippet's src field."""
        s = make_snippet(text="origin test kappa", src="kb")
        result = reciprocal_rank_fusion([[s]])
        assert result[0].retrieval_origins == ["kb"]

    def test_retrieval_origins_two_sources(self) -> None:
        """Same snippet retrieved from both vector and FTS gets both origins."""
        s_kb = make_snippet(
            text="dual origin lambda", src="kb", document_id=30, chunk_ix=0, id=888
        )
        s_ft = make_snippet(
            text="dual origin lambda", src="ft", document_id=30, chunk_ix=0, id=888
        )
        result = reciprocal_rank_fusion([[s_kb], [s_ft]])
        assert len(result) == 1
        origins = result[0].retrieval_origins or []
        assert "kb" in origins
        assert "ft" in origins

    def test_retrieval_origins_none_for_missing_src(self) -> None:
        s = make_snippet(text="no src mu", src=None)
        result = reciprocal_rank_fusion([[s]])
        # src=None should not add an entry; origins list stays empty → None
        assert result[0].retrieval_origins is None

    def test_result_sorted_by_score_descending(self) -> None:
        a = make_snippet(text="rank 1 nu", document_id=41, chunk_ix=0)
        b = make_snippet(text="rank 2 xi", document_id=42, chunk_ix=0)
        c = make_snippet(text="rank 3 omicron", document_id=43, chunk_ix=0)
        # a appears in 3 rankings, b in 2, c in 1 → a > b > c
        result = reciprocal_rank_fusion([[a, b, c], [a, b], [a]])
        scores = [r.rerank_score for r in result]
        assert scores == sorted(scores, reverse=True)
        assert result[0].text == "rank 1 nu"


# ─────────────────────────────────────────────────────────────────────────────
# 4. crossrerank — unit tests (mocked model + profile)
# ─────────────────────────────────────────────────────────────────────────────


class TestCrossrerank:
    def test_fallback_returns_top_candidates_without_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ctx_mod, "_rerank_model", False)
        snippets = [make_snippet(text=f"item {i} pi") for i in range(60)]
        result = crossrerank("query", snippets)
        assert len(result) == RERANK_LIMIT

    def test_empty_input_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_reranker(monkeypatch)
        _patch_profile(monkeypatch, ["term"])
        assert crossrerank("query", []) == []

    def test_text_kind_bonus_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """text chunks get RERANK_KIND_BONUS['text'] boost."""
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["unmatched_term_rho"])
        text_snippet = make_snippet(
            text="text chunk sigma", kind="text", document_id=50, chunk_ix=0
        )
        other_snippet = make_snippet(
            text="other chunk tau", kind="table", document_id=51, chunk_ix=0
        )
        result = crossrerank("query", [text_snippet, other_snippet])
        scores = {r.kind: r.rerank_score for r in result}
        # text bonus +0.12, table has no kind bonus → text should beat table
        assert scores["text"] > scores["table"]
        assert scores["text"] == pytest.approx(
            0.5 + rcnf.RERANK_KIND_BONUS["text"], abs=1e-5
        )

    def test_section_summary_kind_bonus_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["matched_upsilon"])
        # Give summary a term match so zero-overlap penalty doesn't fire
        summary = make_snippet(
            text="section summary matched_upsilon content",
            kind="section_summary",
            document_id=52,
            chunk_ix=0,
        )
        result = crossrerank("query", [summary])
        base_plus_bonus = 0.5 + rcnf.RERANK_KIND_BONUS["section_summary"]
        # Also gets overlap bonus (text match): +1 * RERANK_OVERLAP_WEIGHT
        assert result[0].rerank_score > base_plus_bonus

    def test_zero_overlap_penalty_for_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """section_summary with zero term overlap must be penalised."""
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["jinja", "шаблон"])
        # No query term appears anywhere in this snippet
        irrelevant_summary = make_snippet(
            text="Подготовься к профилю «Искусственный интеллект» НТО",
            kind="section_summary",
            document_id=60,
            chunk_ix=0,
        )
        result = crossrerank("query", [irrelevant_summary])
        expected = (
            0.5
            + rcnf.RERANK_KIND_BONUS["section_summary"]
            - rcnf.RERANK_SUMMARY_ZERO_OVERLAP_PENALTY
        )
        assert result[0].rerank_score == pytest.approx(expected, abs=1e-5)

    def test_zero_overlap_penalty_not_applied_to_text_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The zero-overlap penalty only fires for summary kinds."""
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["absent_term_phi"])
        text_no_overlap = make_snippet(
            text="Нет совпадений",
            kind="text",
            document_id=61,
            chunk_ix=0,
        )
        result = crossrerank("query", [text_no_overlap])
        # text gets +0.12 kind bonus, no penalty
        assert result[0].rerank_score == pytest.approx(
            0.5 + rcnf.RERANK_KIND_BONUS["text"], abs=1e-5
        )

    def test_overlap_bonus_scales_with_match_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """More matched terms → higher overlap bonus (capped at 4)."""
        _patch_reranker(monkeypatch, 0.0)  # base 0 isolates the bonuses
        terms = ["alpha", "beta", "gamma", "delta", "epsilon"]
        _patch_profile(monkeypatch, terms)

        def make_with_n_terms(n: int, doc_id: int) -> Snippet:
            return make_snippet(
                text=" ".join(terms[:n]),
                kind="text",
                document_id=doc_id,
                chunk_ix=0,
            )

        one_match = make_with_n_terms(1, 70)
        two_match = make_with_n_terms(2, 71)
        four_match = make_with_n_terms(4, 72)
        five_match = make_with_n_terms(5, 73)

        result = crossrerank("query", [one_match, two_match, four_match, five_match])
        scores = {r.document_id: r.rerank_score for r in result}

        # More matches → higher score
        assert scores[71] > scores[70]
        assert scores[72] > scores[71]
        # 5 matches capped same as 4 (cap is min(overlap, 4))
        assert scores[73] == pytest.approx(scores[72], abs=1e-5)

    def test_header_text_match_boosts_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["важный"])
        with_header = make_snippet(
            text="прочий текст",
            kind="text",
            header_text="важный заголовок",
            document_id=80,
            chunk_ix=0,
        )
        without_header = make_snippet(
            text="прочий текст",
            kind="text",
            header_text=None,
            document_id=81,
            chunk_ix=0,
        )
        result = crossrerank("query", [with_header, without_header])
        scores = {r.document_id: r.rerank_score for r in result}
        assert scores[80] > scores[81]

    def test_section_path_match_boosts_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["раздел"])
        with_section = make_snippet(
            text="обычный текст",
            kind="text",
            section_path="важный раздел документа",
            document_id=82,
            chunk_ix=0,
        )
        without_section = make_snippet(
            text="обычный текст",
            kind="text",
            section_path=None,
            document_id=83,
            chunk_ix=0,
        )
        result = crossrerank("query", [with_section, without_section])
        scores = {r.document_id: r.rerank_score for r in result}
        assert scores[82] > scores[83]

    def test_entity_terms_match_boosts_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["python"])
        with_entity = make_snippet(
            text="некоторый текст",
            kind="text",
            entity_terms=["python", "django"],
            document_id=84,
            chunk_ix=0,
        )
        without_entity = make_snippet(
            text="некоторый текст",
            kind="text",
            entity_terms=None,
            document_id=85,
            chunk_ix=0,
        )
        result = crossrerank("query", [with_entity, without_entity])
        scores = {r.document_id: r.rerank_score for r in result}
        assert scores[84] > scores[85]

    def test_table_mode_bonus_for_table_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["цена"], table_mode=True)
        table_snippet = make_snippet(
            text="цена: 100 руб",
            kind="table",
            document_id=90,
            chunk_ix=0,
        )
        text_snippet = make_snippet(
            text="цена: 100 руб",
            kind="text",
            document_id=91,
            chunk_ix=0,
        )
        result = crossrerank("query", [table_snippet, text_snippet])
        scores = {r.kind: r.rerank_score for r in result}
        # table gets TABLE_MODE_BONUS +0.20 and text gets KIND_BONUS +0.12
        # so table should win when in table_mode
        assert scores["table"] > scores["text"]

    def test_content_dedup_removes_same_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two snippets from same section with identical text → only one kept."""
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["term"])
        shared_text = "одинаковый текст раздела"
        a = make_snippet(
            text=shared_text,
            kind="text",
            section_path="Intro",
            document_id=100,
            chunk_ix=0,
            src="kb",
        )
        b = make_snippet(
            text=shared_text,
            kind="text",
            section_path="Intro",
            document_id=100,
            chunk_ix=1,
            src="ft",
        )
        result = crossrerank("query", [a, b])
        assert len(result) == 1

    def test_content_dedup_keeps_different_sections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["term"])
        a = make_snippet(
            text="text section A chi",
            kind="text",
            section_path="Section A",
            document_id=101,
            chunk_ix=0,
        )
        b = make_snippet(
            text="text section B psi",
            kind="text",
            section_path="Section B",
            document_id=101,
            chunk_ix=1,
        )
        result = crossrerank("query", [a, b])
        assert len(result) == 2

    def test_per_source_cap_limits_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAX_SNIPPETS_PER_SOURCE=2: no more than 2 chunks from one document."""
        _patch_reranker(monkeypatch, 0.5)
        _patch_profile(monkeypatch, ["term"])
        snippets = [
            make_snippet(
                text=f"chunk {i} omega unique",
                kind="text",
                document_id=200,
                chunk_ix=i,
                src="kb",
            )
            for i in range(5)
        ]
        result = crossrerank("query", snippets)
        from_doc_200 = [r for r in result if r.document_id == 200]
        from vchat.views.chat.ctx import MAX_SNIPPETS_PER_SOURCE

        assert len(from_doc_200) <= MAX_SNIPPETS_PER_SOURCE

    def test_output_sorted_by_rerank_score_descending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reranker(monkeypatch, [0.9, 0.3, 0.6])
        _patch_profile(monkeypatch, ["x"])
        snippets = [
            make_snippet(text="a aleph", document_id=300, chunk_ix=0),
            make_snippet(text="b bet", document_id=301, chunk_ix=0),
            make_snippet(text="c gimel", document_id=302, chunk_ix=0),
        ]
        result = crossrerank("query", snippets)
        scores = [r.rerank_score for r in result]
        assert scores == sorted(scores, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Quality scenarios
#
# Each scenario provides a query, a labelled snippet pool (1=relevant,
# 0=irrelevant), and controlled mock base scores.  We assert that after the
# full boosting pass the relevant snippets rank above the irrelevant ones.
#
# Metrics reported for each scenario:
#   MRR   – mean reciprocal rank (1/rank of first relevant)
#   P@1   – precision at 1 (is rank-1 snippet relevant?)
#   NDCG@3 – normalised DCG at 3
# ─────────────────────────────────────────────────────────────────────────────


def _mrr(ranked: list[Snippet], labels: dict[int, int]) -> float:
    for rank, s in enumerate(ranked, start=1):
        if labels.get(s.id, 0) > 0:
            return 1.0 / rank
    return 0.0


def _precision_at_k(ranked: list[Snippet], labels: dict[int, int], k: int = 1) -> float:
    hits = sum(1 for s in ranked[:k] if labels.get(s.id, 0) > 0)
    return hits / k


def _ndcg_at_k(ranked: list[Snippet], labels: dict[int, int], k: int = 3) -> float:
    def dcg(items: list[Snippet]) -> float:
        return sum(
            (2 ** labels.get(s.id, 0) - 1) / math.log2(i + 2)
            for i, s in enumerate(items[:k])
        )

    ideal_labels = sorted(labels.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal_labels))
    return dcg(ranked) / idcg if idcg > 0 else 0.0


def _run_quality_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    terms: list[str],
    snippets_with_labels: list[tuple[Snippet, int]],
    base_scores: list[float] | float = 0.5,
    table_mode: bool = False,
) -> tuple[list[Snippet], dict[int, int]]:
    """Run RRF + crossrerank with mocked model; return ranked list and labels."""
    _patch_reranker(monkeypatch, base_scores)
    _patch_profile(monkeypatch, terms, table_mode=table_mode)
    snippets = [s for s, _ in snippets_with_labels]
    labels = {s.id: label for s, label in snippets_with_labels}
    fused = reciprocal_rank_fusion([snippets])
    ranked = crossrerank("query", fused)
    return ranked, labels


QUALITY_SCENARIOS = [
    # ── scenario 0: "Jinja шаблоны" — the original bug ──────────────────────
    # Relevant: text chunk about Jinja, contains both query terms.
    # Irrelevant: section_summary from a competition doc that only has "шаблон".
    # Both start at 0.5 base.  After boosting, relevant must win.
    {
        "name": "jinja_templates_vs_irrelevant_summary",
        "terms": ["jinja", "шаблон"],
        "snippets_with_labels": [
            (
                Snippet(
                    id=1001,
                    text="Jinja — шаблонизатор для Python. Шаблоны Jinja поддерживают наследование.",
                    kind="text",
                    src="kb",
                    document_id=1001,
                    chunk_ix=0,
                    uri="https://jinja.palletsprojects.com/",
                    title="Jinja Documentation",
                ),
                1,  # relevant
            ),
            (
                Snippet(
                    id=1002,
                    text="Подготовься к профилю «Искусственный интеллект» НТО! "
                    "За 4 сессии пройди весь соревновательный путь: "
                    "от открытия датасета до загрузки своего решения на лидерборд.",
                    kind="section_summary",
                    src="ft",
                    document_id=1002,
                    chunk_ix=0,
                    uri="https://vector-ai.ru/nto",
                    title="Вектор ИИ",
                ),
                0,  # irrelevant
            ),
        ],
        "base_scores": 0.5,
        "min_mrr": 1.0,
        "min_p1": 1.0,
        "min_ndcg3": 1.0,
    },
    # ── scenario 1: zero-overlap penalty isolates bad summaries ──────────────
    # A section_summary with NO query term overlap must rank below a text
    # chunk that also has no overlap (but is not penalised).
    {
        "name": "zero_overlap_penalty_summary_vs_text",
        "terms": ["python", "декоратор"],
        "snippets_with_labels": [
            (
                Snippet(
                    id=2001,
                    text="Python декоратор используется для обёртки функций.",
                    kind="text",
                    src="kb",
                    document_id=2001,
                    chunk_ix=0,
                    uri="https://docs.python.org/",
                    title="Python docs",
                ),
                1,
            ),
            (
                Snippet(
                    id=2002,
                    text="Полностью нерелевантный документ без совпадений.",
                    kind="section_summary",
                    src="ft",
                    document_id=2002,
                    chunk_ix=0,
                    uri="https://other.example/",
                    title="Other",
                ),
                0,
            ),
        ],
        "base_scores": 0.5,
        "min_mrr": 1.0,
        "min_p1": 1.0,
        "min_ndcg3": 1.0,
    },
    # ── scenario 2: multi-term advantage ─────────────────────────────────────
    # Snippet A has both query terms; snippet B has only one.
    # A must rank above B even with identical base scores.
    {
        "name": "multi_term_advantage",
        "terms": ["async", "await"],
        "snippets_with_labels": [
            (
                Snippet(
                    id=3001,
                    text="В Python async и await используются для асинхронного кода.",
                    kind="text",
                    src="kb",
                    document_id=3001,
                    chunk_ix=0,
                    uri="https://docs.python.org/asyncio",
                    title="asyncio docs",
                ),
                1,
            ),
            (
                Snippet(
                    id=3002,
                    text="Функции могут быть async.",
                    kind="text",
                    src="kb",
                    document_id=3002,
                    chunk_ix=0,
                    uri="https://other.example/async",
                    title="Async intro",
                ),
                0,
            ),
        ],
        "base_scores": 0.5,
        "min_mrr": 1.0,
        "min_p1": 1.0,
        "min_ndcg3": 1.0,
    },
    # ── scenario 3: header_text match lifts relevant snippet ─────────────────
    {
        "name": "header_boost_lifts_relevant",
        "terms": ["индексирование"],
        "snippets_with_labels": [
            (
                Snippet(
                    id=4001,
                    text="Подробности работы БД.",
                    kind="text",
                    src="kb",
                    document_id=4001,
                    chunk_ix=0,
                    header_text="Индексирование в PostgreSQL",
                    uri="https://pg.example/",
                    title="PG docs",
                ),
                1,
            ),
            (
                Snippet(
                    id=4002,
                    text="Подробности работы БД.",
                    kind="text",
                    src="kb",
                    document_id=4002,
                    chunk_ix=0,
                    header_text=None,
                    uri="https://other.example/",
                    title="Other",
                ),
                0,
            ),
        ],
        "base_scores": 0.5,
        "min_mrr": 1.0,
        "min_p1": 1.0,
        "min_ndcg3": 1.0,
    },
    # ── scenario 4: table_mode favours table chunks ───────────────────────────
    {
        "name": "table_mode_prefers_table_kind",
        "terms": ["цена"],
        "snippets_with_labels": [
            (
                Snippet(
                    id=5001,
                    text="цена продукта A: 100 руб\nцена продукта B: 200 руб",
                    kind="table",
                    src="kb",
                    document_id=5001,
                    chunk_ix=0,
                    uri="https://shop.example/",
                    title="Price list",
                ),
                1,
            ),
            (
                Snippet(
                    id=5002,
                    text="цена указана в документации",
                    kind="text",
                    src="kb",
                    document_id=5002,
                    chunk_ix=0,
                    uri="https://docs.example/",
                    title="Docs",
                ),
                0,
            ),
        ],
        "base_scores": 0.5,
        "table_mode": True,
        "min_mrr": 1.0,
        "min_p1": 1.0,
        "min_ndcg3": 1.0,
    },
]


class TestQualityScenarios:
    @pytest.mark.parametrize(
        "scenario", QUALITY_SCENARIOS, ids=[s["name"] for s in QUALITY_SCENARIOS]
    )
    def test_scenario(
        self,
        scenario: dict,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        ranked, labels = _run_quality_scenario(
            monkeypatch,
            terms=scenario["terms"],
            snippets_with_labels=scenario["snippets_with_labels"],
            base_scores=scenario.get("base_scores", 0.5),
            table_mode=scenario.get("table_mode", False),
        )

        mrr = _mrr(ranked, labels)
        p1 = _precision_at_k(ranked, labels, k=1)
        ndcg3 = _ndcg_at_k(ranked, labels, k=3)

        with capsys.disabled():
            print(
                f"\n  [{scenario['name']}]  MRR={mrr:.3f}  P@1={p1:.3f}  NDCG@3={ndcg3:.3f}"
            )
            for rank, s in enumerate(ranked, start=1):
                rel = labels.get(s.id, 0)
                marker = "✓" if rel else "✗"
                print(
                    f"    #{rank} {marker} score={s.rerank_score:.4f}"
                    f"  kind={s.kind}  title={s.title!r}"
                )

        assert mrr >= scenario.get("min_mrr", 0.0), (
            f"MRR {mrr:.3f} below threshold {scenario['min_mrr']}"
        )
        assert p1 >= scenario.get("min_p1", 0.0), (
            f"P@1 {p1:.3f} below threshold {scenario['min_p1']}"
        )
        assert ndcg3 >= scenario.get("min_ndcg3", 0.0), (
            f"NDCG@3 {ndcg3:.3f} below threshold {scenario['min_ndcg3']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Snapshot: score breakdown for each boosting component
#    Runs once, prints a readable table. Never fails — serves as documentation.
# ─────────────────────────────────────────────────────────────────────────────


def test_score_breakdown_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Print how each boosting component contributes to the final score.

    This test always passes; it is a living document for the scoring model.
    Run with -s to see the output.
    """
    _patch_reranker(monkeypatch, 0.0)  # base=0 isolates the bonuses
    terms = ["python", "декоратор"]
    _patch_profile(monkeypatch, terms)

    cases = [
        (
            "text, both terms in text",
            make_snippet(
                text="python декоратор", kind="text", document_id=900, chunk_ix=0
            ),
        ),
        (
            "text, one term in header",
            make_snippet(
                text="python код",
                kind="text",
                header_text="декоратор функции",
                document_id=901,
                chunk_ix=0,
            ),
        ),
        (
            "section_summary, one term overlap",
            make_snippet(
                text="python обзор", kind="section_summary", document_id=902, chunk_ix=0
            ),
        ),
        (
            "section_summary, zero overlap (PENALISED)",
            make_snippet(
                text="нерелевантный текст про ИИ",
                kind="section_summary",
                document_id=903,
                chunk_ix=0,
            ),
        ),
        (
            "table, terms in entity_terms",
            make_snippet(
                text="строки таблицы",
                kind="table",
                entity_terms=["python", "декоратор"],
                document_id=904,
                chunk_ix=0,
            ),
        ),
    ]

    snippets = [s for _, s in cases]
    result = crossrerank("query", snippets)
    score_by_doc = {r.document_id: r.rerank_score for r in result}

    with capsys.disabled():
        print("\n  ── Score breakdown (base=0.0) ──────────────────────────────────")
        print(f"  {'Case':<46} {'Score':>7}")
        print("  " + "─" * 54)
        for label, s in cases:
            sc = score_by_doc.get(s.document_id, float("nan"))
            print(f"  {label:<46} {sc:>7.4f}")
        print()
        print(f"  Field weights  : {rcnf.RERANK_FIELD_WEIGHTS}")
        print(f"  Overlap weight : {rcnf.RERANK_OVERLAP_WEIGHT} × min(n, 4)")
        print(f"  Kind bonuses   : {rcnf.RERANK_KIND_BONUS}")
        print(f"  Table mode     : {rcnf.RERANK_TABLE_MODE_BONUS}")
        print(
            f"  Zero-overlap Δ : -{rcnf.RERANK_SUMMARY_ZERO_OVERLAP_PENALTY} (summary/section_summary only)"
        )
        print(f"  VECTOR_MAX_DIST: {VECTOR_MAX_DIST}")
        print(f"  RERANK_LIMIT   : {RERANK_LIMIT}")
