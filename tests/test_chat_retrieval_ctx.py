"""
Tests and quality benchmarks for the retrieval pipeline.

Coverage:
  - vchat/views/chat/retrieval_config.py — constant definitions
  - reciprocal_rank_fusion — RRF scores and origin tracking
  - crossrerank — boosting logic, dedup, penalties
  - System constants — VECTOR_MAX_DIST, RERANK_LIMIT
  - Quality scenarios — ranking correctness under controlled mock scores
"""

from __future__ import annotations

import math
import pytest

from vchat.views.chat.ctx import (
    Snippet,
    VECTOR_MAX_DIST,
    RERANK_LIMIT,
    RRF_K,
    reciprocal_rank_fusion,
    crossrerank,
)
from vchat.views.chat import retrieval_config as rcnf


# --------------------------------------------------------------------------- #
# Helpers & Fixtures
# --------------------------------------------------------------------------- #


def make_snippet(**kwargs) -> Snippet:
    """Create a test Snippet with sensible defaults."""
    defaults = dict(
        id=1,
        text="default text",
        kind="text",
        src="kb",
        document_id=1,
        chunk_ix=0,
        uri="https://example.com/doc",
        title="Test doc",
        header_text=None,
        section_path=None,
        entity_terms=None,
    )
    defaults.update(kwargs)
    return Snippet(**defaults)


class UniformReranker:
    """Mock cross-encoder that returns uniform base scores."""

    def __init__(self, score: float = 0.5):
        self.score = score

    def predict(self, pairs, show_progress_bar=False):
        return [self.score] * len(pairs)


def make_profile(
    terms,
    table_mode=False,
    quote_mode=False,
    enumeration_mode=False,
) -> dict:
    """Create a queryprofile dict for testing."""
    return {
        "lexical_query": " OR ".join(terms),
        "lexical_terms": terms,
        "table_mode": table_mode,
        "quote_mode": quote_mode,
        "enumeration_mode": enumeration_mode,
    }


@pytest.fixture
def uniform_reranker(monkeypatch):
    """Patch the reranker global to return uniform 0.5 scores."""
    model = UniformReranker(0.5)
    monkeypatch.setattr("vchat.views.chat.ctx._rerank_model", model)
    return model


def patch_profile(monkeypatch, terms, **flags):
    """Monkeypatch queryprofile to return a fixed profile."""
    profile = make_profile(terms, **flags)
    monkeypatch.setattr("vchat.views.chat.ctx.queryprofile", lambda q: profile)
    return profile


# --------------------------------------------------------------------------- #
# Section 1: retrieval_config Constants
# --------------------------------------------------------------------------- #


class TestRetrievalConfig:
    """Tests for retrieval_config.py constants and weights."""

    def test_field_weights_all_positive(self):
        """All field weight bonuses should be positive."""
        for field, weight in rcnf.RERANK_FIELD_WEIGHTS.items():
            assert weight > 0, f"{field} weight should be positive, got {weight}"

    def test_field_weights_reasonable_order(self):
        """Weights should reflect importance: header > section > entity."""
        assert (
            rcnf.RERANK_FIELD_WEIGHTS["header_text"]
            > rcnf.RERANK_FIELD_WEIGHTS["section_path"]
        )
        assert (
            rcnf.RERANK_FIELD_WEIGHTS["section_path"]
            > rcnf.RERANK_FIELD_WEIGHTS["entity_terms"]
        )

    def test_overlap_weight_positive(self):
        """Per-term overlap weight should be positive."""
        assert rcnf.RERANK_OVERLAP_WEIGHT > 0

    def test_kind_bonus_text_positive(self):
        """Text kind should get positive bonus."""
        assert rcnf.RERANK_KIND_BONUS["text"] > 0

    def test_kind_bonus_summary_positive(self):
        """Summary kind should get positive bonus."""
        assert rcnf.RERANK_KIND_BONUS["summary"] > 0

    def test_table_mode_bonus_positive(self):
        """Table mode bonuses should be positive."""
        for kind in rcnf.RERANK_TABLE_MODE_BONUS.values():
            assert kind > 0

    def test_zero_overlap_penalty_positive(self):
        """Zero-overlap penalty magnitude should be positive."""
        assert rcnf.RERANK_SUMMARY_ZERO_OVERLAP_PENALTY > 0


# --------------------------------------------------------------------------- #
# Section 2: System Constants
# --------------------------------------------------------------------------- #


class TestSystemConstants:
    """Tests for key constants that affect retrieval quality."""

    def test_vector_max_dist_is_068(self):
        """VECTOR_MAX_DIST should be tightened from 0.78 to 0.68."""
        assert VECTOR_MAX_DIST == 0.68, f"Expected 0.68, got {VECTOR_MAX_DIST}"

    def test_rerank_limit_doubled_from_24_to_48(self):
        """RERANK_LIMIT should be increased for better candidate pool."""
        assert RERANK_LIMIT == 48, f"Expected 48, got {RERANK_LIMIT}"

    def test_rrf_k_denominator_constant(self):
        """RRF_K should be stable constant."""
        assert RRF_K == 60


# --------------------------------------------------------------------------- #
# Section 3: Reciprocal Rank Fusion
# --------------------------------------------------------------------------- #


class TestReciprocalRankFusion:
    """Tests for RRF score computation and origin tracking."""

    def test_single_ranking_computes_score_correctly(self):
        """RRF with one ranking should compute 1/(k+rank) for each."""
        s1 = make_snippet(id=1, text="first")
        s2 = make_snippet(id=2, text="second")
        ranking = [s1, s2]

        result = reciprocal_rank_fusion([ranking])

        assert len(result) == 2
        # First snippet: 1/(60+1) = 1/61 ≈ 0.0164
        assert abs(result[0].rerank_score - 1 / 61) < 1e-4
        # Second snippet: 1/(60+2) = 1/62 ≈ 0.0161
        assert abs(result[1].rerank_score - 1 / 62) < 1e-4

    def test_two_rankings_fuse_and_deduplicate(self):
        """RRF should fuse multiple rankings and deduplicate by key."""
        s1 = make_snippet(id=1, text="shared content", document_id=10)
        s2 = make_snippet(id=2, text="unique to first", document_id=11)
        s3 = make_snippet(id=3, text="unique to second", document_id=12)

        result = reciprocal_rank_fusion([[s1, s2], [s1, s3]])

        # Should have 3 unique snippets
        assert len(result) == 3
        # s1 appears in both rankings, so its score is higher
        assert result[0].id == 1  # Ranked first due to higher RRF score

    def test_retrieval_origins_tracked_from_src(self):
        """RRF should populate retrieval_origins from snippet.src values."""
        s1 = make_snippet(id=1, text="content", src="kb")
        s2 = make_snippet(id=1, text="content", src="ft")  # Same id, different src

        result = reciprocal_rank_fusion([[s1], [s2]])

        assert len(result) == 1
        snippet = result[0]
        assert snippet.retrieval_origins is not None
        assert "kb" in snippet.retrieval_origins
        assert "ft" in snippet.retrieval_origins

    def test_retrieval_origins_no_duplicates(self):
        """RRF should not duplicate origins in the list."""
        s = make_snippet(id=1, text="content", src="kb")
        # Same snippet in both rankings
        result = reciprocal_rank_fusion([[s], [s]])

        assert len(result) == 1
        assert result[0].retrieval_origins == ["kb"]

    def test_empty_text_snippets_skipped(self):
        """RRF should skip snippets with empty text."""
        s1 = make_snippet(id=1, text="real content")
        s2 = make_snippet(id=2, text="   ")  # Whitespace only

        result = reciprocal_rank_fusion([[s1, s2]])

        assert len(result) == 1
        assert result[0].id == 1


# --------------------------------------------------------------------------- #
# Section 4: Crossrerank with Mock Model
# --------------------------------------------------------------------------- #


class TestCrossrankLogic:
    """Tests for crossrerank boosting logic with mocked reranker."""

    def test_fallback_without_model(self, monkeypatch):
        """Should fall back to RRF limit when model unavailable."""
        monkeypatch.setattr("vchat.views.chat.ctx._rerank_model", False)
        monkeypatch.setattr(
            "vchat.views.chat.ctx.queryprofile", lambda q: make_profile([])
        )

        snippets = [make_snippet(id=i, text=f"snippet {i}") for i in range(50)]
        result = crossrerank("query", snippets)

        assert len(result) == RERANK_LIMIT  # Should limit to RERANK_LIMIT

    def test_zero_overlap_penalty_for_summary(self, uniform_reranker, monkeypatch):
        """Summary with zero term overlap should receive penalty."""
        terms = ["python", "type", "hint"]
        patch_profile(monkeypatch, terms)

        # Summary with NO matching terms
        summary_no_match = make_snippet(
            id=1,
            kind="section_summary",
            text="This section discusses unrelated topics",
        )
        # Summary WITH matching terms
        summary_match = make_snippet(
            id=2,
            kind="section_summary",
            text="Python type hints are useful for static analysis",
        )

        result = crossrerank("", [summary_no_match, summary_match])

        # Matched summary should rank higher despite same base score
        assert result[0].id == 2, "Summary with term matches should rank first"
        assert result[1].id == 1, "Summary with zero overlap should rank second"

    def test_overlap_bonus_scales_with_count(self, uniform_reranker, monkeypatch):
        """Overlap bonus should scale with number of matched terms (capped at 4)."""
        terms = ["jinja", "template", "render", "html", "extra"]
        patch_profile(monkeypatch, terms)

        # Snippet with 1 term
        s1 = make_snippet(id=1, kind="text", text="Jinja is a templating engine")
        # Snippet with 3 terms
        s2 = make_snippet(
            id=2,
            kind="text",
            text="Jinja templates render HTML with dynamic content",
        )

        result = crossrerank("", [s1, s2])

        # More overlaps = higher boost
        assert result[0].id == 2, "Snippet with more term matches should rank first"
        assert result[0].rerank_score > result[1].rerank_score

    def test_text_kind_bonus(self, uniform_reranker, monkeypatch):
        """Text chunks should get higher kind bonus than summaries."""
        terms = ["content"]
        patch_profile(monkeypatch, terms)

        text_snippet = make_snippet(id=1, kind="text", text="content here")
        summary_snippet = make_snippet(id=2, kind="summary", text="content here")

        result = crossrerank("", [text_snippet, summary_snippet])

        # Text should rank higher due to higher kind bonus
        assert result[0].id == 1
        assert result[0].rerank_score > result[1].rerank_score, (
            "Text should have higher score than summary with same base"
        )

    def test_table_mode_bonus_applied(self, uniform_reranker, monkeypatch):
        """Table/table_rows should get bonus when table_mode is active."""
        terms = ["data"]
        patch_profile(monkeypatch, terms, table_mode=True)

        text_snippet = make_snippet(id=1, kind="text", text="data")
        table_snippet = make_snippet(id=2, kind="table_rows", text="data")

        result = crossrerank("", [text_snippet, table_snippet])

        # Table should rank higher in table_mode
        assert result[0].id == 2

    def test_content_level_dedup_same_section(self, uniform_reranker, monkeypatch):
        """Content dedup should prevent exact duplicates from same section."""
        terms = ["topic"]
        patch_profile(monkeypatch, terms)

        # Two snippets from same section with identical content (real duplication)
        identical_text = "topic discussion content"
        s1 = make_snippet(
            id=1,
            kind="text",
            section_path="Section A",
            text=identical_text,
        )
        s2 = make_snippet(
            id=2,
            kind="text",
            section_path="Section A",
            text=identical_text,
        )

        result = crossrerank("", [s1, s2])

        # Should only return one (content dedup prevents duplicate text)
        assert len(result) == 1
        assert result[0].id == 1

    def test_per_source_cap_still_applies(self, uniform_reranker, monkeypatch):
        """Per-source cap (MAX_SNIPPETS_PER_SOURCE) should still limit results."""
        terms = ["term"]
        patch_profile(monkeypatch, terms)

        # Many snippets from same source
        snippets = [
            make_snippet(id=i, src="kb", document_id=1, text="term") for i in range(5)
        ]

        result = crossrerank("", snippets)

        # Should not exceed MAX_SNIPPETS_PER_SOURCE per source
        kb_count = sum(1 for s in result if s.src == "kb")
        assert kb_count <= 2  # MAX_SNIPPETS_PER_SOURCE = 2


# --------------------------------------------------------------------------- #
# Section 5: Quality Scenarios (Ranking Correctness)
# --------------------------------------------------------------------------- #


class TestRankingQuality:
    """Quality benchmarks with realistic scenarios."""

    def test_scenario_jinja_templates_ranks_relevant_higher(
        self, uniform_reranker, monkeypatch
    ):
        """
        The "Вектор ИИ" scenario: irrelevant snippet should rank below relevant.

        Query: "Расскажи мне про Jinja шаблоны"
        - Relevant: Text about Jinja templates with both terms in text
        - Irrelevant: Section summary from "Вектор ИИ" with only "шаблон"
        """
        terms = ["jinja", "шаблон"]
        patch_profile(monkeypatch, terms)

        relevant = make_snippet(
            id=1,
            kind="text",
            title="Jinja Template Engine",
            section_path="Web Frameworks",
            text="Jinja is a powerful template engine for Python. Шаблоны позволяют генерировать HTML динамически.",
        )

        irrelevant = make_snippet(
            id=2,
            kind="section_summary",
            title="Вектор ИИ: Подготовься к профилю",
            section_path="Competitions",
            text="Подготовься к профилю «Искусственный интеллект» по шаблону решений. За 4 сессии пройди весь соревновательный путь.",
        )

        result = crossrerank("Расскажи мне про Jinja шаблоны", [irrelevant, relevant])

        assert result[0].id == 1, (
            "Relevant Jinja snippet should rank above irrelevant 'Вектор ИИ' snippet"
        )
        assert result[0].rerank_score > result[1].rerank_score, (
            "Relevant should have higher score"
        )

    def test_scenario_python_type_hints_zero_overlap_penalty(
        self, uniform_reranker, monkeypatch
    ):
        """Zero-overlap penalty should demote summaries with no matching terms."""
        terms = ["python", "type", "hint"]
        patch_profile(monkeypatch, terms)

        relevant_summary = make_snippet(
            id=1,
            kind="section_summary",
            text="Python type hints provide static type checking capabilities",
        )

        irrelevant_summary = make_snippet(
            id=2,
            kind="section_summary",
            text="This section covers unrelated topics about web development",
        )

        result = crossrerank(
            "python type hints", [irrelevant_summary, relevant_summary]
        )

        assert result[0].id == 1, "Summary with matching terms should rank first"
        assert result[1].id == 2, "Summary with zero overlap should rank last"

    def test_scenario_multi_term_advantage(self, uniform_reranker, monkeypatch):
        """Snippets with more term matches should rank higher."""
        terms = ["database", "query", "optimization", "performance"]
        patch_profile(monkeypatch, terms)

        # Only 1 term
        low_overlap = make_snippet(
            id=1,
            kind="text",
            text="Database administration is important",
        )

        # 3 terms
        high_overlap = make_snippet(
            id=2,
            kind="text",
            text="Query optimization improves database performance significantly",
        )

        result = crossrerank(
            "database query optimization performance", [low_overlap, high_overlap]
        )

        assert result[0].id == 2
        assert result[0].rerank_score > result[1].rerank_score


# --------------------------------------------------------------------------- #
# Section 6: Quality Benchmark Summary
# --------------------------------------------------------------------------- #


def compute_ndcg_at_k(ranking, relevance_labels, k=5):
    """
    Compute NDCG@k for a ranking.

    ranking: list of (snippet_id, score) tuples in ranked order
    relevance_labels: dict[snippet_id] -> relevance (0-3)
    k: cutoff rank
    """
    idcg = sum(
        (2**label - 1) / math.log2(i + 2)
        for i, label in enumerate(sorted(relevance_labels.values(), reverse=True)[:k])
    )
    dcg = sum(
        (2 ** relevance_labels.get(s_id, 0) - 1) / math.log2(i + 2)
        for i, (s_id, _score) in enumerate(ranking[:k])
    )
    return dcg / idcg if idcg > 0 else 0.0


class TestQualityBenchmark:
    """Benchmark that demonstrates quality improvements."""

    @pytest.mark.parametrize(
        "scenario_name,query,snippets,relevance",
        [
            (
                "jinja_templates",
                "jinja шаблоны",
                [
                    ("relevant_jinja", "text", "Jinja template engine uses шаблоны"),
                    (
                        "irrelevant_vector",
                        "section_summary",
                        "Вектор ИИ шаблон решения",
                    ),
                ],
                {"relevant_jinja": 3, "irrelevant_vector": 0},
            ),
            (
                "python_types",
                "python type hints",
                [
                    (
                        "relevant_types",
                        "text",
                        "Python type hints improve code quality",
                    ),
                    ("irrelevant_web", "section_summary", "Web development frameworks"),
                ],
                {"relevant_types": 3, "irrelevant_web": 0},
            ),
        ],
    )
    def test_benchmark_quality_scenarios(
        self,
        scenario_name,
        query,
        snippets,
        relevance,
        uniform_reranker,
        monkeypatch,
        capsys,
    ):
        """Benchmark test: verify relevant snippets rank above irrelevant."""
        terms = query.split()
        patch_profile(monkeypatch, terms)

        snippet_objs = []
        for sid, kind, text in snippets:
            snippet_objs.append(make_snippet(id=sid, kind=kind, text=text))

        result = crossrerank(query, snippet_objs)

        # Extract ranking: list of (id, score) tuples
        ranking = [(s.id, s.rerank_score or 0) for s in result]

        # Compute NDCG@5
        ndcg = compute_ndcg_at_k(ranking, relevance)

        # For binary relevance, NDCG should be 1.0 if ranking is perfect
        assert ndcg > 0.8, f"{scenario_name}: NDCG should be high, got {ndcg:.3f}"

        # Print summary for inspection
        print(f"\nScenario: {scenario_name}")
        print(f"Query: {query}")
        print(f"NDCG@5: {ndcg:.3f}")
        for i, (s_id, score) in enumerate(ranking, 1):
            rel = relevance.get(s_id, 0)
            print(f"  {i}. {s_id} (rel={rel}, score={score:.4f})")
