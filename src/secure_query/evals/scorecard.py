"""Adjudicated head-to-head scorecard: Secure Query vs Databricks Genie.

Uses the same LiveCase / Attempt / summarise grader as the product eval.
Genie rows are scored against reference SQL (no trust of Genie SQL). Flip-rate
is computed across repeats for each system independently.

Usage:
  python -m secure_query.evals.scorecard --suite path/to/cases.json --repeat 5

Requires GENIE_SPACE_ID + Databricks credentials for the Genie side. Secure Query
side uses the local planner + DuckDB/warehouse configured in env.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from secure_query.engine.env import load_dotenv
from secure_query.evals.accuracy import (
    ABSTAINED,
    CORRECT,
    ERROR,
    PREVIEW_ROWS,
    ROW_CAP,
    WRONG,
    Attempt,
    LiveCase,
    LiveCaseResult,
    RowRunner,
    _matches,
    _reference_rows,
    _results_match,
    run_live_case,
    summarise,
)
from secure_query.evals.genie import GenieAnswer, GenieError, ask_genie, genie_configured
from secure_query.kernel.trace import answer_fingerprint
from secure_query.planner.sql_plan import plan_sql_question


@dataclass
class SystemStats:
    name: str
    total: int
    correct: int
    wrong: int
    abstained: int
    error: int
    unsafe: int
    answer_rate: float
    wrong_rate: float
    wrong_rate_ci95: tuple[float, float]
    flip_rate: float | None
    over_refusal: float | None


def _stats_from_results(name: str, results: list[LiveCaseResult]) -> SystemStats:
    summary = summarise(results)
    answerable = [r for r in results if r.case.expect == "answer"]
    answered = [r for r in answerable if r.verdict == CORRECT]
    over_refused = [r for r in answerable if r.verdict == ABSTAINED]
    return SystemStats(
        name=name,
        total=summary["total"],
        correct=summary["correct"],
        wrong=summary["wrong"] + summary["unsafe"],
        abstained=summary["abstained"],
        error=summary["error"],
        unsafe=summary["unsafe"],
        answer_rate=(len(answered) / len(answerable)) if answerable else 0.0,
        wrong_rate=summary["wrong_rate"],
        wrong_rate_ci95=tuple(summary["wrong_rate_ci95"]),
        flip_rate=summary.get("flip_rate"),
        over_refusal=(len(over_refused) / len(answerable)) if answerable else None,
    )


def grade_genie_answer(
    case: LiveCase,
    answer: GenieAnswer,
    *,
    reference_rows: list[tuple[Any, ...]] | None,
    db_path: Path | None = None,
    run_sql: RowRunner | None = None,
) -> Attempt:
    """Map a GenieAnswer onto an Attempt with the same matcher Secure Query is graded by.

    A reply with no SQL result (a clarifying question or prose) is a refusal, not a
    wrong answer: both systems get the same column-order, float and LIMIT-tie tolerance.
    """
    if answer.status in {"failed", "cancelled", "timeout"}:
        return Attempt(ERROR, f"genie {answer.status}: {answer.text or ''}")
    if answer.sql is None and not answer.rows:
        return Attempt(ABSTAINED, "genie returned no query result", refusal_code="genie_no_result")
    fp = answer_fingerprint(answer.rows)
    if case.expect == "abstain":
        return Attempt(WRONG, "genie answered a should-abstain case", fingerprint=fp, sql=answer.sql)
    if reference_rows is None:
        return Attempt(ERROR, "no reference rows to grade genie")
    actual = list(answer.rows)
    if db_path is not None or run_sql is not None:
        right = _matches(case, actual, reference_rows, db_path or Path(), run_sql)
    else:  # no reference database: same matcher minus the LIMIT-tie check
        right = _results_match(
            actual,
            reference_rows,
            ordered=case.ordered,
            extra_columns_ok=case.extra_columns_ok,
            subset_columns_ok=case.subset_columns_ok,
        )
    if right:
        return Attempt(CORRECT, "matches reference", fingerprint=fp, sql=answer.sql)
    return Attempt(
        WRONG,
        "row mismatch vs reference",
        fingerprint=fp,
        sql=answer.sql,
        rows=tuple(tuple(row) for row in actual[:PREVIEW_ROWS]),
        row_count=len(actual),
    )


def run_genie_suite(
    cases: list[LiveCase],
    *,
    db_path: Path,
    repeat: int,
    run_sql: RowRunner | None = None,
) -> list[LiveCaseResult]:
    results: list[LiveCaseResult] = []
    for case in cases:
        ref_rows = None
        if case.expect == "answer" and case.reference_sql:
            try:
                ref_rows = _reference_rows(case, db_path, run_sql)
            except Exception:  # noqa: BLE001 — graded as ERROR below, never as a Genie miss
                ref_rows = None
        attempts: list[Attempt] = []
        for _ in range(max(1, repeat)):
            try:
                answer = ask_genie(case.question)
                attempts.append(
                    grade_genie_answer(case, answer, reference_rows=ref_rows, db_path=db_path, run_sql=run_sql)
                )
            except GenieError as exc:
                attempts.append(Attempt(ERROR, str(exc)))
        results.append(LiveCaseResult(case=case, attempts=attempts))
    return results


def databricks_runner(tenant_id: str) -> RowRunner:
    """Run SQL text on the warehouse both systems are compared on.

    Reference SQL runs unvalidated here. That is acceptable only because this is eval-only
    and the token (SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN) must be SELECT-only.
    """
    import os

    from secure_query.auth import Principal
    from secure_query.engine.databricks import execute_databricks
    from secure_query.engine.execute import ExecuteOptions
    from secure_query.kernel.compile import CompiledQuery

    token = (os.environ.get("SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("--backend databricks needs SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN (a SELECT-only token)")
    principal = Principal(principal_id="eval", tenant_id=tenant_id, warehouse_token=token)

    def run_sql(sql: str) -> list[tuple[Any, ...]]:
        compiled = CompiledQuery(sql=sql, plan_hash="eval", sql_hash="eval", parameters=[])
        return execute_databricks(compiled, principal=principal, options=ExecuteOptions(max_rows=ROW_CAP)).rows

    run_sql.dialect = "databricks"  # type: ignore[attr-defined]
    return run_sql


def export_cases(
    results: list[LiveCaseResult], *, db_path: Path, run_sql: RowRunner | None = None
) -> list[dict[str, Any]]:
    """Every case with its attempts, in the shape `evals.run --json` writes.

    A wrong case also carries the question, the reference SQL and the first rows of both
    answers, so `scripts/adjudicate.py packets` needs no database (the warehouse may not
    be reachable when a person rates, and Genie's answer cannot be re-run).
    """
    from secure_query.evals.run import _case_report

    exported = []
    for result in results:
        record = _case_report(result)
        record["attempts"] = [
            {
                "verdict": a.verdict,
                "detail": a.detail,
                "sql": a.sql,
                "fingerprint": a.fingerprint,
                "refusal_code": a.refusal_code,
            }
            for a in result.attempts
        ]
        decisive = next((a for a in result.attempts if a.verdict == result.verdict), None)
        if result.verdict == WRONG and decisive is not None:
            case = result.case
            reference: dict[str, Any] | None = None
            if case.reference_sql:
                try:
                    rows = _reference_rows(case, db_path, run_sql)
                    reference = {"count": len(rows), "first": [list(r) for r in rows[:PREVIEW_ROWS]]}
                except Exception as exc:  # noqa: BLE001 — the export must not lose the case
                    reference = {"error": f"{type(exc).__name__}: {exc}"}
            record["adjudication"] = {
                "question": case.question,
                "reference_sql": case.reference_sql,
                "reference_result": reference,
                "system_result": {
                    "count": decisive.row_count,
                    "first": [list(r) for r in decisive.rows or ()],
                },
            }
        exported.append(record)
    return exported


def beats_genie(sq: SystemStats, genie: SystemStats) -> dict[str, Any]:
    """Success bar from PLAN: wrong-rate 95% UB ≤ 2% AND below Genie; answer-rate ≥ 70%."""
    sq_ub = sq.wrong_rate_ci95[1]
    return {
        "sq_wrong_ub_le_2pct": sq_ub <= 0.02,
        "sq_wrong_below_genie": sq.wrong_rate < genie.wrong_rate,
        "sq_answer_rate_ge_70pct": sq.answer_rate >= 0.70,
        "pass": sq_ub <= 0.02 and sq.wrong_rate < genie.wrong_rate and sq.answer_rate >= 0.70,
    }


def format_scorecard(sq: SystemStats, genie: SystemStats | None) -> str:
    lines = [
        "Adjudicated head-to-head scorecard",
        f"{'metric':<28} {'secure_query':>14} {'genie':>14}",
        "-" * 58,
    ]
    rows = [
        ("n", sq.total, genie.total if genie else None),
        ("wrong_rate", f"{sq.wrong_rate:.1%}", f"{genie.wrong_rate:.1%}" if genie else "—"),
        (
            "wrong_rate_95ub",
            f"{sq.wrong_rate_ci95[1]:.1%}",
            f"{genie.wrong_rate_ci95[1]:.1%}" if genie else "—",
        ),
        ("answer_rate", f"{sq.answer_rate:.1%}", f"{genie.answer_rate:.1%}" if genie else "—"),
        (
            "flip_rate",
            f"{sq.flip_rate:.1%}" if sq.flip_rate is not None else "—",
            f"{genie.flip_rate:.1%}" if genie and genie.flip_rate is not None else "—",
        ),
        (
            "over_refusal",
            f"{sq.over_refusal:.1%}" if sq.over_refusal is not None else "—",
            f"{genie.over_refusal:.1%}" if genie and genie.over_refusal is not None else "—",
        ),
    ]
    for label, a, b in rows:
        lines.append(f"{label:<28} {str(a):>14} {str(b) if b is not None else '—':>14}")
    if genie is not None:
        gate = beats_genie(sq, genie)
        lines.append("-" * 58)
        lines.append(f"pass bar: {gate['pass']}  ({json.dumps(gate)})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Secure Query vs Genie scorecard")
    parser.add_argument("--suite", required=True, help="Accuracy suite JSON (same as evals.run)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sq-only", action="store_true", help="Skip Genie (print SQ stats only)")
    parser.add_argument("--genie-only", action="store_true", help="Skip Secure Query side")
    parser.add_argument("--out", type=Path, default=None, help="Write JSON scorecard")
    parser.add_argument("--provider", default=None)
    parser.add_argument(
        "--backend",
        choices=("duckdb", "databricks"),
        default="duckdb",
        help="Where reference SQL and Secure Query run. Use databricks for a head-to-head: "
        "Genie answers from the warehouse, so everything else must read it too.",
    )
    args = parser.parse_args(argv)

    from secure_query.evals.run import _client, _load_domain_suite
    from secure_query.planner import PlannerError
    from secure_query.planner.llm import CHECK_TEMPERATURE

    catalog, db_path, cases = _load_domain_suite(args.suite)  # same loader as evals.run
    if args.limit:
        cases = cases[: args.limit]
    run_sql: RowRunner | None = None
    if args.backend == "databricks":
        try:
            run_sql = databricks_runner(catalog.tenant_id)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        catalog = catalog.model_copy(update={"sql_dialect": "databricks"})
    elif not db_path.exists() and not args.genie_only:
        print(f"database missing: {db_path}", file=sys.stderr)
        return 2

    sq_results: list[LiveCaseResult] = []
    if not args.genie_only:
        try:
            client = _client(args.provider)
            # The product gate (service.ask) plans every question twice; score that system.
            check_client = _client(args.provider, CHECK_TEMPERATURE)
        except PlannerError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for case in cases:
            sq_results.append(
                run_live_case(
                    case,
                    catalog,
                    client,
                    db_path,
                    repeats=args.repeat,
                    planner=plan_sql_question,
                    check_client=check_client,
                    run_sql=run_sql,
                )
            )

    genie_results: list[LiveCaseResult] = []
    if not args.sq_only:
        if not genie_configured():
            print(
                "Genie not configured (DATABRICKS_HOST, GENIE_TOKEN|DATABRICKS_TOKEN, "
                "GENIE_SPACE_ID). Pass --sq-only or set credentials.",
                file=sys.stderr,
            )
            if args.genie_only:
                return 2
        else:
            genie_results = run_genie_suite(cases, db_path=db_path, repeat=args.repeat, run_sql=run_sql)

    sq_stats = _stats_from_results("secure_query", sq_results) if sq_results else None
    genie_stats = _stats_from_results("genie", genie_results) if genie_results else None
    if sq_stats is None and genie_stats is None:
        return 2
    if sq_stats is None:
        sq_stats = SystemStats(
            name="secure_query",
            total=0,
            correct=0,
            wrong=0,
            abstained=0,
            error=0,
            unsafe=0,
            answer_rate=0.0,
            wrong_rate=0.0,
            wrong_rate_ci95=(0.0, 0.0),
            flip_rate=None,
            over_refusal=None,
        )
    print(format_scorecard(sq_stats, genie_stats))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "secure_query": asdict(sq_stats),
                    "genie": asdict(genie_stats) if genie_stats else None,
                    "gate": beats_genie(sq_stats, genie_stats) if genie_stats else None,
                    "suite": str(args.suite),
                    "backend": args.backend,
                    "repeat": args.repeat,
                    "schema": catalog.planner_summary(),
                    "systems": {
                        name: {"cases": export_cases(results, db_path=db_path, run_sql=run_sql)}
                        for name, results in (("secure_query", sq_results), ("genie", genie_results))
                        if results
                    },
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
