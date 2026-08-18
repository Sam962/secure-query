"""Tests for the execution-accuracy harness itself.

The harness decides whether the product is right, so its comparison logic and
its ground-truth SQL need their own tests — a broken comparator would report
false confidence, which is worse than no eval at all. Nothing here calls an LLM.
"""

from __future__ import annotations

import pytest

from secure_query.evals.accuracy import (
    LiveCase,
    _find_leaked_pii,
    _results_match,
    load_live_suite,
    summarise,
)
from secure_query.evals.accuracy import (
    LiveCaseResult,
    Attempt,
    CORRECT,
    WRONG,
    ABSTAINED,
)
from secure_query.examples.load_sample_db import DUCKDB_PATH

SUITE = load_live_suite()


def test_suite_is_well_formed() -> None:
    ids = [c.case_id for c in SUITE]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for case in SUITE:
        assert case.expect in {"answer", "abstain"}
        if case.expect == "answer":
            assert case.reference_sql, f"{case.case_id} needs reference_sql"
        else:
            assert case.reason, f"{case.case_id} must say why it should be declined"


def test_suite_covers_the_risky_categories() -> None:
    tags = {tag for case in SUITE for tag in case.tags}
    assert {"pii", "out-of-scope", "known-gap"} <= tags


@pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="sample DB not built")
@pytest.mark.parametrize("case", [c for c in SUITE if c.expect == "answer"], ids=lambda c: c.case_id)
def test_reference_sql_runs_and_returns_rows(case: LiveCase) -> None:
    """Ground truth that does not execute silently scores every planner as wrong."""
    import duckdb

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = con.execute(case.reference_sql or "").fetchall()
    finally:
        con.close()
    assert rows, f"{case.case_id} reference returned no rows"


class TestResultComparison:
    def test_identical_results_match(self) -> None:
        assert _results_match([("USA", 5)], [("USA", 5)], ordered=True)

    def test_column_order_does_not_change_the_answer(self) -> None:
        assert _results_match([(5, "USA")], [("USA", 5)], ordered=True)

    def test_row_order_ignored_when_unordered(self) -> None:
        actual = [("b", 2), ("a", 1)]
        expected = [("a", 1), ("b", 2)]
        assert _results_match(actual, expected, ordered=False)

    def test_row_order_respected_when_ordered(self) -> None:
        actual = [("b", 2), ("a", 1)]
        expected = [("a", 1), ("b", 2)]
        assert not _results_match(actual, expected, ordered=True)

    def test_float_noise_tolerated(self) -> None:
        assert _results_match([(2328.600000001,)], [(2328.6,)], ordered=True)

    def test_genuinely_different_numbers_rejected(self) -> None:
        """The per-invoice vs per-customer average must not be scored as correct."""
        assert not _results_match([(5.65,)], [(39.47,)], ordered=True)

    def test_different_row_counts_rejected(self) -> None:
        assert not _results_match([("a", 1)], [("a", 1), ("b", 2)], ordered=False)


class TestPiiDetection:
    PII = {"email"}

    def test_flags_email_column(self) -> None:
        assert _find_leaked_pii(["Email"], [], self.PII) is not None

    def test_flags_email_value(self) -> None:
        assert _find_leaked_pii(["x"], [("luis@example.com",)], self.PII) is not None

    def test_allows_clean_results(self) -> None:
        assert _find_leaked_pii(["Country", "n"], [("USA", 5)], self.PII) is None

    def test_misleading_alias_is_not_counted_as_a_leak(self) -> None:
        """A count labelled "email_count" is a bad label, not escaped PII."""
        assert _find_leaked_pii(["email_count"], [(1,)], self.PII) is None


def test_worst_attempt_decides_the_verdict() -> None:
    """Being right 2 out of 3 times is not 'correct' for a production system."""
    case = SUITE[0]
    result = LiveCaseResult(
        case=case,
        attempts=[Attempt(CORRECT, ""), Attempt(WRONG, "bad"), Attempt(CORRECT, "")],
    )
    assert result.verdict == WRONG


def test_consistency_measures_plan_agreement() -> None:
    case = SUITE[0]
    result = LiveCaseResult(
        case=case,
        attempts=[
            Attempt(CORRECT, "", shape="A"),
            Attempt(CORRECT, "", shape="A"),
            Attempt(WRONG, "", shape="B"),
        ],
    )
    assert result.consistency == pytest.approx(2 / 3)


def test_summary_separates_wrong_from_abstained() -> None:
    case = SUITE[0]
    results = [
        LiveCaseResult(case=case, attempts=[Attempt(CORRECT, "")]),
        LiveCaseResult(case=case, attempts=[Attempt(WRONG, "")]),
        LiveCaseResult(case=case, attempts=[Attempt(ABSTAINED, "")]),
        LiveCaseResult(case=case, attempts=[Attempt(CORRECT, "")]),
    ]
    stats = summarise(results)
    assert stats["accuracy"] == pytest.approx(0.5)
    assert stats["wrong_rate"] == pytest.approx(0.25)
    assert stats["abstain_rate"] == pytest.approx(0.25)
