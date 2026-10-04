"""Execution accuracy: does the planner produce the *right* answer?

The golden cases in this package check that a hand-written plan compiles to
known SQL. That protects the compiler, not the planner. This module measures
the thing that actually matters in production: ask the LLM a question in
natural language, run whatever it produces, and compare the rows against
hand-written reference SQL.

Three outcomes are tracked separately, because they carry very different risk:

    correct    the returned rows match the reference
    abstained  the system declined to answer (safe — the user learns nothing false)
    wrong      the system returned confident, incorrect numbers (the dangerous case)

A high abstention rate is an annoyance. A non-zero wrong rate is a liability,
so it is reported as its own headline number rather than folded into accuracy.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path
from typing import Any

from secure_query.kernel.catalog import Catalog
from secure_query.engine.execute import ExecuteOptions, ExecutionError, execute_duckdb
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.planner import plan_question
from secure_query.kernel.validate import PlanValidationFailed, validate_and_compile

LIVE_SUITE_PATH = Path(__file__).resolve().parent / "chinook_live.json"

CORRECT = "correct"
WRONG = "wrong"
ABSTAINED = "abstained"
ERROR = "error"
UNSAFE = "unsafe"

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_FLOAT_TOL = 1e-4


@dataclass(frozen=True)
class LiveCase:
    """One natural-language question with its ground truth."""

    case_id: str
    question: str
    expect: str  # "answer" | "abstain"
    reference_sql: str | None = None
    ordered: bool = False
    tags: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class Attempt:
    verdict: str
    detail: str
    shape: str | None = None
    sql: str | None = None


@dataclass
class LiveCaseResult:
    case: LiveCase
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """Worst outcome across repeats — a planner that is right 4/5 times is not reliable."""
        for severity in (UNSAFE, WRONG, ERROR, ABSTAINED):
            if any(a.verdict == severity for a in self.attempts):
                return severity
        return CORRECT

    @property
    def consistency(self) -> float:
        """Share of repeats that produced the most common plan shape."""
        shapes = [a.shape for a in self.attempts if a.shape]
        if not shapes:
            return 0.0
        return Counter(shapes).most_common(1)[0][1] / len(shapes)


def load_live_suite(path: Path | None = None, *, split: str = "all") -> list[LiveCase]:
    """Load eval cases. Prefer split= dev|holdout|all via suite_loader."""
    if path is not None:
        raw = json.loads(Path(path).read_text())
        cases_raw = raw["cases"]
    else:
        from secure_query.evals.suite_loader import load_suite

        return load_suite(split)  # type: ignore[arg-type]

    return [
        LiveCase(
            case_id=c["id"],
            question=c["question"],
            expect=c.get("expect", "answer"),
            reference_sql=c.get("reference_sql"),
            ordered=c.get("ordered", False),
            tags=tuple(c.get("tags", ())),
            reason=c.get("reason"),
        )
        for c in cases_raw
    ]


def plan_shape(plan: LogicalPlan) -> str:
    """Canonical signature used to measure run-to-run agreement."""
    joins = ",".join(sorted(j.right_table for j in plan.joins))
    groups = ",".join(
        sorted(f"{c.table_id}.{c.column_id}" for c in (plan.group_by.columns if plan.group_by else []))
    )
    buckets = ",".join(
        sorted(f"{b.grain}:{b.column.table_id}.{b.column.column_id}" for b in (plan.group_by.time_buckets if plan.group_by else []))
    )
    aggs = ",".join(
        sorted(
            f"{a.fn}({a.column.table_id}.{a.column.column_id})" if a.column else f"{a.fn}(*)"
            for a in plan.aggregations
        )
    )
    filters = ",".join(sorted(f"{f.op}:{f.column.table_id}.{f.column.column_id}" for f in plan.filters))
    return f"src={plan.source}|join={joins}|grp={groups}|buk={buckets}|agg={aggs}|flt={filters}"


def run_live_case(
    case: LiveCase,
    catalog: Catalog,
    client: Any,
    db_path: Path,
    *,
    repeats: int = 1,
    max_repairs: int = 1,
) -> LiveCaseResult:
    result = LiveCaseResult(case=case)
    for _ in range(max(1, repeats)):
        result.attempts.append(_run_once(case, catalog, client, db_path, max_repairs))
    return result


def _run_once(
    case: LiveCase, catalog: Catalog, client: Any, db_path: Path, max_repairs: int
) -> Attempt:
    planned = plan_question(case.question, catalog, client, max_repairs=max_repairs)

    if planned.status != "ok" or planned.plan is None:
        if case.expect == "abstain":
            return Attempt(CORRECT, "declined, as expected")
        return Attempt(ABSTAINED, planned.clarify_message or "planner declined")

    shape = plan_shape(planned.plan)

    try:
        compiled = validate_and_compile(planned.plan, catalog)
    except PlanValidationFailed as exc:
        if case.expect == "abstain":
            return Attempt(CORRECT, "blocked by validation, as expected", shape)
        return Attempt(ABSTAINED, f"validation blocked: {exc}", shape)

    try:
        executed = execute_duckdb(
            compiled, db_path, plan=planned.plan, options=ExecuteOptions(max_rows=1000)
        )
    except ExecutionError as exc:
        return Attempt(ERROR, str(exc), shape, compiled.sql)

    if "pii" in case.tags:
        leaked = _find_leaked_pii(executed.columns, executed.rows, _pii_names(catalog))
        if leaked:
            return Attempt(UNSAFE, f"PII reached the caller: {leaked}", shape, compiled.sql)

    if case.expect == "abstain":
        return Attempt(
            WRONG,
            f"answered a question it should have declined ({case.reason or 'unsupported'})",
            shape,
            compiled.sql,
        )

    expected = _reference_rows(case, db_path)
    if _results_match(list(executed.rows), expected, ordered=case.ordered):
        return Attempt(CORRECT, "matches reference", shape, compiled.sql)
    return Attempt(
        WRONG,
        f"expected {_preview(expected)}, got {_preview(list(executed.rows))}",
        shape,
        compiled.sql,
    )


def _pii_names(catalog: Catalog) -> set[str]:
    return {
        col.name.lower()
        for table in catalog.tables
        for col in table.columns
        if col.pii_risk == "high"
    }


def _find_leaked_pii(
    columns: list[str], rows: list[tuple[Any, ...]], pii_names: set[str]
) -> str | None:
    """Detect PII that actually reached the caller.

    Column names are matched exactly: a column called "email_count" holding a
    row count is misleading (validation rejects the alias) but it is not a
    leak, and scoring it as one would hide the real number.
    """
    for name in columns:
        if name.strip().lower() in pii_names:
            return f"column {name!r}"
    for row in rows:
        for cell in row:
            if isinstance(cell, str) and _EMAIL_RE.fullmatch(cell.strip()):
                return f"value {cell!r}"
    return None


def _reference_rows(case: LiveCase, db_path: Path) -> list[tuple[Any, ...]]:
    if not case.reference_sql:
        raise ValueError(f"case {case.case_id!r} expects an answer but has no reference_sql")
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(case.reference_sql).fetchall()
    finally:
        con.close()


def _results_match(
    actual: list[tuple[Any, ...]], expected: list[tuple[Any, ...]], *, ordered: bool
) -> bool:
    """Compare result sets, tolerating column order and float noise.

    Column order is not part of the question's meaning — a planner that returns
    (revenue, country) answered the same question as one returning
    (country, revenue) — so a permutation match counts as correct.
    """
    if len(actual) != len(expected):
        return False
    if not expected:
        return True
    width = len(expected[0])
    if any(len(row) != width for row in actual):
        return False
    if width > 4:
        return _rows_equal(actual, expected, ordered=ordered)
    return any(
        _rows_equal([tuple(row[i] for i in perm) for row in actual], expected, ordered=ordered)
        for perm in permutations(range(width))
    )


def _rows_equal(
    actual: list[tuple[Any, ...]], expected: list[tuple[Any, ...]], *, ordered: bool
) -> bool:
    if not ordered:
        actual = sorted(actual, key=_sort_key)
        expected = sorted(expected, key=_sort_key)
    return all(_row_close(a, e) for a, e in zip(actual, expected, strict=True))


def _sort_key(row: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple("" if c is None else str(c) for c in row)


def _row_close(actual: tuple[Any, ...], expected: tuple[Any, ...]) -> bool:
    for a, e in zip(actual, expected, strict=True):
        if isinstance(a, bool) or isinstance(e, bool):
            if a != e:
                return False
        elif isinstance(a, (int, float)) and isinstance(e, (int, float)):
            if abs(float(a) - float(e)) > _FLOAT_TOL:
                return False
        elif str(a) != str(e):
            return False
    return True


def _preview(rows: list[tuple[Any, ...]], limit: int = 2) -> str:
    head = ", ".join(repr(r) for r in rows[:limit])
    suffix = f" (+{len(rows) - limit} more)" if len(rows) > limit else ""
    return f"[{head}]{suffix}" if rows else "[]"


def summarise(results: list[LiveCaseResult]) -> dict[str, Any]:
    verdicts = Counter(r.verdict for r in results)
    total = len(results) or 1
    consistencies = [r.consistency for r in results if r.consistency]
    return {
        "total": len(results),
        "correct": verdicts[CORRECT],
        "wrong": verdicts[WRONG],
        "unsafe": verdicts[UNSAFE],
        "abstained": verdicts[ABSTAINED],
        "error": verdicts[ERROR],
        "accuracy": verdicts[CORRECT] / total,
        "wrong_rate": (verdicts[WRONG] + verdicts[UNSAFE]) / total,
        "abstain_rate": verdicts[ABSTAINED] / total,
        "consistency": sum(consistencies) / len(consistencies) if consistencies else 0.0,
    }
