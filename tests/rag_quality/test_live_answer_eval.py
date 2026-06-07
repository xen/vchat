from __future__ import annotations

import pytest

from tests.rag_quality import answer_eval, live_answer_eval


def test_select_cases_defaults_to_limit() -> None:
    cases = answer_eval.load_cases()

    selected = live_answer_eval.select_cases(
        cases,
        names=[],
        limit=2,
        run_all=False,
    )

    assert [case["name"] for case in selected] == [
        cases[0]["name"],
        cases[1]["name"],
    ]


def test_select_cases_filters_by_name_and_case_type() -> None:
    cases = answer_eval.load_cases()

    selected = live_answer_eval.select_cases(
        cases,
        names=["negative_query_absent", "exact_fact_lookup_course_start"],
        limit=1,
        run_all=False,
    )

    assert {case["name"] for case in selected} == {
        "exact_fact_lookup_course_start",
        "metadata_only_absent_body_fact",
        "negative_absent_answer_no_ios_app",
    }


def test_select_cases_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="Unknown eval case"):
        live_answer_eval.select_cases(
            answer_eval.load_cases(),
            names=["missing-case"],
            limit=1,
            run_all=False,
        )
