"""Tests for dev/holdout eval split discipline."""

from secure_query.evals.dev_expansion import HOLDOUT_CASE_IDS
from secure_query.evals.suite_loader import load_suite, suite_stats


def test_holdout_is_frozen_subset() -> None:
    holdout = load_suite("holdout")
    assert len(holdout) == len(HOLDOUT_CASE_IDS)
    assert {c.case_id for c in holdout} == HOLDOUT_CASE_IDS


def test_dev_suite_has_100_plus_cases() -> None:
    stats = suite_stats("dev")
    assert stats["total"] >= 100


def test_splits_are_disjoint() -> None:
    dev_ids = {c.case_id for c in load_suite("dev")}
    holdout_ids = {c.case_id for c in load_suite("holdout")}
    assert dev_ids.isdisjoint(holdout_ids)


def test_all_includes_both() -> None:
    assert suite_stats("all")["total"] == suite_stats("dev")["total"] + suite_stats("holdout")["total"]


def test_answer_cases_have_non_trivial_references() -> None:
    """A reference that returns nothing or a lone 0 lets a wrong filter score as correct."""
    from pathlib import Path

    import duckdb
    import pytest

    from secure_query.evals.suite_loader import load_suite

    db = Path("data/chinook.duckdb")
    if not db.exists():
        pytest.skip("sample DB not loaded")
    con = duckdb.connect(str(db), read_only=True)
    trivial = []
    for case in load_suite("all"):
        if case.expect != "answer":
            continue
        rows = con.execute(case.reference_sql).fetchall()
        if not rows or (len(rows) == 1 and all(v in (0, None) for v in rows[0])):
            trivial.append(case.case_id)
    assert trivial == []
