"""Phase 4 — Chinook golden plan → SQL (+ optional row) tests."""

from __future__ import annotations

import pytest

from secure_query.evals.golden import iter_cases, run_golden_case


@pytest.mark.parametrize("case_dir", iter_cases(), ids=lambda p: p.name)
def test_chinook_golden(case_dir) -> None:
    result = run_golden_case(case_dir)
    assert result.passed, "\n".join(result.errors) or result.case_id
