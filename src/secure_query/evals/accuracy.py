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
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path
from typing import Any

from secure_query.engine.execute import ExecuteOptions, ExecutionError, execute_duckdb
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.validate import PlanValidationFailed, validate_and_compile
from secure_query.planner import PlannerError, plan_question

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
    reference_db: str | None = None
    """SQLite file the reference SQL runs on (benchmarks whose gold is SQLite SQL)."""
    extra_columns_ok: bool = False
    """Correct if the reference columns appear among the result's (same rows).
    For benchmarks whose gold SQL selects one column where a list plan returns
    the whole row; any other difference is still wrong."""


@dataclass(frozen=True)
class Attempt:
    verdict: str
    detail: str
    shape: str | None = None
    sql: str | None = None
    refusal_code: str | None = None
    """Which control declined (clarify_code), when the system refused."""
    blocked: str | None = None
    """On an answerable case a guard refused: would its blocked plan have been right?
    "right" or "wrong"; None when there was no plan to score (e.g. model refusal)."""
    fingerprint: str | None = None
    """Order-insensitive hash of the returned rows, for agreement analysis."""


@dataclass
class LiveCaseResult:
    case: LiveCase
    attempts: list[Attempt] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    """Extra plans sampled at temperature > 0 (self-consistency analysis); see _sample."""

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


def parse_cases(raw_cases: list[dict[str, Any]]) -> list[LiveCase]:
    return [
        LiveCase(
            case_id=c["id"],
            question=c["question"],
            expect=c.get("expect", "answer"),
            reference_sql=c.get("reference_sql"),
            ordered=c.get("ordered", False),
            tags=tuple(c.get("tags", ())),
            reason=c.get("reason"),
            extra_columns_ok=c.get("extra_columns_ok", False),
        )
        for c in raw_cases
    ]


def load_live_suite(path: Path) -> list[LiveCase]:
    """Cases from a suite JSON file ({"cases": [...]}). Chinook splits: suite_loader.load_suite."""
    return parse_cases(json.loads(Path(path).read_text(encoding="utf-8"))["cases"])


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
    samples: int = 0,
    sample_client: Any = None,
) -> LiveCaseResult:
    result = LiveCaseResult(case=case)
    for _ in range(max(1, repeats)):
        result.attempts.append(_run_once(case, catalog, client, db_path, max_repairs))
    for _ in range(samples):
        result.samples.append(_sample(case, catalog, sample_client, db_path, max_repairs))
    return result


def _sample(
    case: LiveCase, catalog: Catalog, client: Any, db_path: Path, max_repairs: int
) -> dict[str, Any]:
    """One extra plan through the full pipeline: its shape, rows and verdict."""
    try:
        planned = plan_question(case.question, catalog, client, max_repairs=max_repairs)
    except PlannerError as exc:
        return {"status": "error", "detail": str(exc)}
    if planned.status != "ok" or planned.compiled is None or planned.plan is None:
        return {"status": "refused", "code": planned.clarify_code}
    verdict, fingerprint = _judge(case, planned.compiled, db_path)
    return {
        "status": "answered" if verdict else "error",
        "shape": plan_shape(planned.plan),
        "fingerprint": fingerprint,
        "verdict": verdict,
    }


def _run_once(
    case: LiveCase, catalog: Catalog, client: Any, db_path: Path, max_repairs: int
) -> Attempt:
    try:
        planned = plan_question(case.question, catalog, client, max_repairs=max_repairs)
    except PlannerError as exc:
        # Transport failure (e.g. Ollama gone after the laptop slept): score this
        # case as an error and keep going instead of losing the whole run.
        return Attempt(ERROR, f"planner error: {exc}")

    if planned.status != "ok" or planned.plan is None:
        if planned.refused_by_model:
            code = "planner_refusal"
        else:
            code = planned.clarify_code or (
                "planner_refusal" if planned.refused else "validation_failed"
            )
        if case.expect == "abstain":
            return Attempt(CORRECT, "declined, as expected", refusal_code=code)
        blocked = _judge(case, planned.blocked, db_path)[0] if planned.blocked else None
        return Attempt(
            ABSTAINED,
            planned.clarify_message or "planner declined",
            refusal_code=code,
            blocked=blocked,
        )

    shape = plan_shape(planned.plan)

    try:
        compiled = validate_and_compile(planned.plan, catalog)
    except PlanValidationFailed as exc:
        if case.expect == "abstain":
            return Attempt(
                CORRECT, "blocked by validation, as expected", shape, refusal_code="validation_failed"
            )
        return Attempt(ABSTAINED, f"validation blocked: {exc}", shape, refusal_code="validation_failed")

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
    actual = list(executed.rows)
    fingerprint = _fingerprint(actual)
    if _matches(case, actual, expected, db_path):
        return Attempt(CORRECT, "matches reference", shape, compiled.sql, fingerprint=fingerprint)
    return Attempt(
        WRONG,
        f"expected {_preview(expected)}, got {_preview(actual)}",
        shape,
        compiled.sql,
        fingerprint=fingerprint,
    )


def _judge(case: LiveCase, compiled: Any, db_path: Path) -> tuple[str | None, str | None]:
    """Execute a compiled plan and compare with the reference: ("right"|"wrong"|None, fingerprint)."""
    try:
        executed = execute_duckdb(compiled, db_path, options=ExecuteOptions(max_rows=1000))
    except ExecutionError:
        return None, None
    actual = list(executed.rows)
    verdict = "right" if _matches(case, actual, _reference_rows(case, db_path), db_path) else "wrong"
    return verdict, _fingerprint(actual)


def _matches(
    case: LiveCase, actual: list[tuple[Any, ...]], expected: list[tuple[Any, ...]], db_path: Path
) -> bool:
    if _results_match(actual, expected, ordered=case.ordered, extra_columns_ok=case.extra_columns_ok):
        return True
    unlimited = _unlimited_reference_rows(case, db_path)
    return unlimited is not None and _valid_tie_resolution(actual, expected, unlimited)


def _fingerprint(rows: list[tuple[Any, ...]]) -> str:
    """Hash of the rows ignoring row and column order, floats rounded: equal answers agree."""
    import hashlib

    def cell(v: Any) -> str:
        return f"{float(v):.4f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)

    canon = sorted("|".join(sorted(cell(c) for c in row)) for row in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]


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
    return _run_reference(case, case.reference_sql, db_path)


def _unlimited_reference_rows(case: LiveCase, db_path: Path) -> list[tuple[Any, ...]] | None:
    """Reference rows with LIMIT removed, or None when the reference has no LIMIT."""
    import sqlglot

    dialect = "sqlite" if case.reference_db else "duckdb"
    tree = sqlglot.parse_one(case.reference_sql or "", read=dialect)
    if not tree.args.get("limit"):
        return None
    tree.set("limit", None)
    return _run_reference(case, tree.sql(dialect=dialect), db_path)


def _run_reference(case: LiveCase, sql: str, db_path: Path) -> list[tuple[Any, ...]]:
    """Reference SQL runs on `case.reference_db` (SQLite) when set, else on the DuckDB file."""
    if case.reference_db:
        import sqlite3

        con = sqlite3.connect(f"file:{case.reference_db}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _valid_tie_resolution(
    actual: list[tuple[Any, ...]],
    expected: list[tuple[Any, ...]],
    unlimited: list[tuple[Any, ...]],
) -> bool:
    """A LIMIT that cuts through a tie may legally return any of the tied rows.

    "Top 3 billing countries" when France and Brazil tie for third has two right
    answers, and DuckDB picks one arbitrarily per execution. Accept `actual` when
    every row is a real row of the un-LIMITed reference and its numeric values
    (the measures being ranked) match the reference top-N exactly. Swapping a
    tied row keeps that profile; returning a lower-ranked row does not.
    """
    if len(actual) != len(expected) or not expected or len(unlimited) <= len(expected):
        return False
    width = len(expected[0])
    if any(len(row) != width for row in actual):
        return False
    target = _numeric_profile(expected)
    orders = permutations(range(width)) if width <= 4 else [tuple(range(width))]
    for perm in orders:
        rows = [tuple(row[i] for i in perm) for row in actual]
        if _numeric_profile(rows) != target:
            continue
        if all(any(_row_close(r, u) for u in unlimited) for r in rows):
            return True
    return False


def _numeric_profile(rows: list[tuple[Any, ...]]) -> list[tuple[float, ...]]:
    return sorted(
        tuple(
            round(float(c), 4)
            for c in row
            if isinstance(c, (int, float)) and not isinstance(c, bool)
        )
        for row in rows
    )


def _results_match(
    actual: list[tuple[Any, ...]],
    expected: list[tuple[Any, ...]],
    *,
    ordered: bool,
    extra_columns_ok: bool = False,
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
    if extra_columns_ok and actual and len(actual[0]) > width:
        return _contains_columns(actual, expected, ordered=ordered)
    if any(len(row) != width for row in actual):
        return False
    if width > 4:
        return _rows_equal(actual, expected, ordered=ordered)
    return any(
        _rows_equal([tuple(row[i] for i in perm) for row in actual], expected, ordered=ordered)
        for perm in permutations(range(width))
    )


def _contains_columns(
    actual: list[tuple[Any, ...]], expected: list[tuple[Any, ...]], *, ordered: bool
) -> bool:
    """True if some choice of distinct actual columns reproduces the expected rows.

    Candidates per expected column are pruned by comparing the column's sorted
    values first, so only plausible mappings are tried row by row.
    """
    from itertools import product

    def column(rows: list[tuple[Any, ...]], i: int) -> list[tuple[Any, ...]]:
        return sorted(((r[i],) for r in rows), key=_sort_key)

    candidates = []
    for j in range(len(expected[0])):
        want = column(expected, j)
        options = [
            i
            for i in range(len(actual[0]))
            if all(_row_close(a, e) for a, e in zip(column(actual, i), want, strict=True))
        ]
        if not options:
            return False
        candidates.append(options)
    for mapping in product(*candidates):
        if len(set(mapping)) < len(mapping):
            continue
        projected = [tuple(row[i] for i in mapping) for row in actual]
        if _rows_equal(projected, expected, ordered=ordered):
            return True
    return False


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


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Unlike the normal approximation it stays sensible at 0/n: 0 wrong out of 12
    still allows a true wrong-rate up to ~24%, which is the point of reporting it.
    """
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _refusal_code(result: LiveCaseResult) -> str | None:
    return next((a.refusal_code for a in reversed(result.attempts) if a.refusal_code), None)


def summarise(results: list[LiveCaseResult]) -> dict[str, Any]:
    """Headline rates plus the breakdowns needed to argue about them.

    answer_rate / over_refusal_rate are over *answerable* cases only (expect=answer);
    mixing in expected refusals would let a system that refuses everything look fine.
    by_refusal_code scores each control: `correct` refusals were on questions that
    should be declined. On answerable questions, a guard's blocked plan is run:
    `blocked_wrong` means the guard stopped a wrong answer, `blocked_right` means it
    cost a right one; `false` is an answerable refusal with no plan to score.
    """
    verdicts = Counter(r.verdict for r in results)
    total = len(results) or 1
    consistencies = [r.consistency for r in results if r.consistency]
    wrong_total = verdicts[WRONG] + verdicts[UNSAFE]
    wrong_low, wrong_high = wilson_interval(wrong_total, len(results))

    answerable = [r for r in results if r.case.expect == "answer"]
    answered_right = sum(1 for r in answerable if r.verdict == CORRECT)
    refused_answerable = sum(1 for r in answerable if r.verdict == ABSTAINED)

    by_tag: dict[str, Counter[str]] = {}
    for r in results:
        for tag in r.case.tags or ("untagged",):
            by_tag.setdefault(tag, Counter())[r.verdict] += 1

    by_code: dict[str, Counter[str]] = {}
    for r in results:
        code = _refusal_code(r)
        if code is None:
            continue
        bucket = by_code.setdefault(code, Counter())
        if r.case.expect == "abstain":
            bucket["correct"] += 1
            continue
        blocked = next((a.blocked for a in reversed(r.attempts) if a.refusal_code), None)
        bucket[{"right": "blocked_right", "wrong": "blocked_wrong"}.get(blocked, "false")] += 1

    return {
        "total": len(results),
        "correct": verdicts[CORRECT],
        "wrong": verdicts[WRONG],
        "unsafe": verdicts[UNSAFE],
        "abstained": verdicts[ABSTAINED],
        "error": verdicts[ERROR],
        "accuracy": verdicts[CORRECT] / total,
        "wrong_rate": wrong_total / total,
        "wrong_rate_ci95": (wrong_low, wrong_high),
        "abstain_rate": verdicts[ABSTAINED] / total,
        "answerable": len(answerable),
        "answer_rate": answered_right / len(answerable) if answerable else 0.0,
        "over_refusal_rate": refused_answerable / len(answerable) if answerable else 0.0,
        "by_tag": {tag: dict(counts) for tag, counts in sorted(by_tag.items())},
        "by_refusal_code": {code: dict(c) for code, c in sorted(by_code.items())},
        "consistency": sum(consistencies) / len(consistencies) if consistencies else 0.0,
    }
