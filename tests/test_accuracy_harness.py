"""Tests for the execution-accuracy harness itself.

The harness decides whether the product is right, so its comparison logic and
its ground-truth SQL need their own tests — a broken comparator would report
false confidence, which is worse than no eval at all. Nothing here calls an LLM.
"""

from __future__ import annotations

import pytest

from secure_query.demo.load_chinook import DUCKDB_PATH
from secure_query.evals.accuracy import (
    ABSTAINED,
    CORRECT,
    WRONG,
    Attempt,
    LiveCase,
    LiveCaseResult,
    _find_leaked_pii,
    _results_match,
    summarise,
)
from secure_query.evals.suite_loader import load_suite

SUITE = load_suite()


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


@pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="sample DB not built")
@pytest.mark.parametrize("case", [c for c in SUITE if c.sql_reference], ids=lambda c: c.case_id)
def test_sql_reference_runs_and_returns_one_row_or_more(case: LiveCase) -> None:
    import duckdb

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = con.execute(case.sql_reference or "").fetchall()
    finally:
        con.close()
    assert rows and rows[0][0] is not None, f"{case.case_id} sql_reference returned nothing"


def test_sql_planner_scores_ir_gap_cases_against_sql_reference() -> None:
    import json

    from secure_query.evals.accuracy import run_live_case
    from secure_query.planner import MockLLMClient
    from secure_query.planner.sql_plan import plan_sql_question

    case = LiveCase(
        case_id="x", question="How many invoices are there?", expect="abstain", reason="IR gap",
        sql_reference="SELECT COUNT(*) FROM Invoice",
    )
    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    from secure_query.demo.chinook import sample_catalog

    client = MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) AS n FROM Invoice"})])
    result = run_live_case(case, sample_catalog(), client, DUCKDB_PATH, planner=plan_sql_question)
    assert result.case.expect == "answer" and result.verdict == CORRECT


class TestResultComparison:
    def test_text_time_bucket_matches_its_start_date(self) -> None:
        from datetime import datetime

        assert _results_match([("2022-03", 7)], [(datetime(2022, 3, 1), 7)], ordered=False)
        assert _results_match([("2022", 7)], [(datetime(2022, 1, 1), 7)], ordered=False)
        assert not _results_match([("2022-04", 7)], [(datetime(2022, 3, 1), 7)], ordered=False)
        assert not _results_match([("2022", 7)], [(datetime(2022, 3, 1), 7)], ordered=False)

    def test_subset_columns_only_when_allowed(self) -> None:
        expected = [("Opera", 1)]
        assert not _results_match([("Opera",)], expected, ordered=False)
        assert _results_match([("Opera",)], expected, ordered=False, subset_columns_ok=True)
        assert not _results_match([("Jazz",)], expected, ordered=False, subset_columns_ok=True)


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


def test_tie_at_limit_accepts_either_tied_row_but_not_a_lower_one() -> None:
    from secure_query.evals.accuracy import _valid_tie_resolution

    unlimited = [("USA", 91), ("Canada", 56), ("France", 35), ("Brazil", 35), ("Germany", 28)]
    expected = [("USA", 91), ("Canada", 56), ("France", 35)]
    assert _valid_tie_resolution([("USA", 91), ("Canada", 56), ("Brazil", 35)], expected, unlimited)
    assert _valid_tie_resolution([(35, "Brazil"), (91, "USA"), (56, "Canada")], expected, unlimited)
    assert not _valid_tie_resolution([("USA", 91), ("Canada", 56), ("Germany", 28)], expected, unlimited)
    assert not _valid_tie_resolution([("USA", 91), ("Canada", 56), ("Brazil", 99)], expected, unlimited)
    assert not _valid_tie_resolution([("USA", 91), ("Canada", 56)], expected, unlimited)


def test_unlimited_reference_strips_limit_only_when_present(tmp_path) -> None:
    import duckdb

    from secure_query.evals.accuracy import LiveCase, _unlimited_reference_rows

    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a', 2), ('b', 1), ('c', 1)) v(k, n)")
    con.close()
    limited = LiveCase("x", "q", "answer", reference_sql="SELECT k, n FROM t ORDER BY n DESC LIMIT 2")
    assert len(_unlimited_reference_rows(limited, db) or []) == 3
    plain = LiveCase("y", "q", "answer", reference_sql="SELECT k, n FROM t")
    assert _unlimited_reference_rows(plain, db) is None


def test_transport_failure_scores_error_and_does_not_abort_run() -> None:
    from pathlib import Path

    from secure_query.demo.chinook import sample_catalog
    from secure_query.evals.accuracy import ERROR, LiveCase, run_live_case
    from secure_query.planner import PlannerError

    class DownClient:
        def complete(self, messages):
            raise PlannerError("LLM unreachable")

    case = LiveCase("c", "How many invoices are there in total?", "answer", reference_sql="SELECT 1")
    result = run_live_case(case, sample_catalog(), DownClient(), Path("unused.duckdb"))
    assert result.verdict == ERROR


def test_wilson_bound_is_honest_about_small_samples() -> None:
    from secure_query.evals.accuracy import wilson_interval

    low, high = wilson_interval(0, 12)
    assert low == 0.0 and high == pytest.approx(0.2425, abs=1e-3)
    assert wilson_interval(0, 200)[1] < 0.02 < wilson_interval(0, 150)[1]
    low, high = wilson_interval(2, 12)
    assert low < 2 / 12 < high


def test_summary_scores_refusals_per_control_on_answerable_cases_only() -> None:
    from secure_query.evals.accuracy import LiveCase

    answer = LiveCase("a", "q", "answer", reference_sql="SELECT 1", tags=("topn",))
    decline = LiveCase("d", "q", "abstain", tags=("pii",))
    results = [
        LiveCaseResult(case=answer, attempts=[Attempt(CORRECT, "")]),
        LiveCaseResult(case=answer, attempts=[Attempt(ABSTAINED, "", refusal_code="out_of_scope")]),
        LiveCaseResult(case=decline, attempts=[Attempt(CORRECT, "", refusal_code="restricted_pii")]),
        LiveCaseResult(case=decline, attempts=[Attempt(CORRECT, "", refusal_code="out_of_scope")]),
    ]
    stats = summarise(results)
    assert stats["answerable"] == 2
    assert stats["answer_rate"] == pytest.approx(0.5)
    assert stats["over_refusal_rate"] == pytest.approx(0.5)
    assert stats["by_refusal_code"] == {
        "out_of_scope": {"correct": 1, "false": 1},
        "restricted_pii": {"correct": 1},
    }
    assert stats["by_tag"]["topn"] == {"correct": 1, "abstained": 1}
    assert stats["wrong_rate_ci95"][0] == 0.0


def test_model_refusal_is_not_credited_to_a_guard() -> None:
    import json
    from pathlib import Path

    from secure_query.demo.chinook import sample_catalog
    from secure_query.evals.accuracy import LiveCase, run_live_case
    from secure_query.planner import MockLLMClient

    reason = "The question needs a table or column that is not in the catalog."
    client = MockLLMClient(responses=[json.dumps({"cannot_answer": True, "reason": reason})])
    case = LiveCase("c", "How many tracks are there?", "answer", reference_sql="SELECT 1")
    result = run_live_case(case, sample_catalog(), client, Path("unused.duckdb"))
    assert result.attempts[0].refusal_code == "planner_refusal"


@pytest.mark.skipif(not DUCKDB_PATH.exists(), reason="sample DB not built")
def test_guard_refusal_is_scored_by_running_the_blocked_plan() -> None:
    """A refusal on an answerable case is only a cost if the blocked plan was right."""
    import json

    from secure_query.demo.chinook import sample_catalog
    from secure_query.evals.accuracy import run_live_case
    from secure_query.planner import MockLLMClient

    # "How many invoices from Brazil?" answered without the Brazil filter:
    # dropped_literals refuses, and the plan it blocked counts every invoice.
    unfiltered = json.dumps(
        {
            "source": "Invoice",
            "filters": [],
            "group_by": None,
            "aggregations": [{"fn": "count", "column": None, "alias": "n"}],
            "having": [],
            "order_by": [],
            "limit": 1,
        }
    )
    question = "How many invoices were billed to Brazil?"
    stopped = LiveCase(
        "s", question, "answer",
        reference_sql="SELECT COUNT(*) FROM Invoice WHERE BillingCountry = 'Brazil'",
    )
    cost = LiveCase("c", question, "answer", reference_sql="SELECT COUNT(*) FROM Invoice")
    for case, expected in ((stopped, "blocked_wrong"), (cost, "blocked_right")):
        result = run_live_case(case, sample_catalog(), MockLLMClient([unfiltered]), DUCKDB_PATH)
        assert result.verdict == ABSTAINED
        assert summarise([result])["by_refusal_code"] == {"dropped_filter": {expected: 1}}
