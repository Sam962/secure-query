"""Chinook Phase 4 evals — golden plans + optional live planner scoring."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.logical_plan import LogicalPlan
from secure_query.validate import validate, validate_and_compile

CHINOOK_EVAL_DIR = Path(__file__).resolve().parent / "chinook"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    sql: str | None = None


def iter_cases(root: Path = CHINOOK_EVAL_DIR) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "case.json").exists())


def load_case(case_dir: Path) -> tuple[dict[str, Any], LogicalPlan]:
    meta = json.loads((case_dir / "case.json").read_text())
    plan = LogicalPlan.model_validate_json((case_dir / "plan.json").read_text())
    return meta, plan


def normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().split())


def run_golden_case(case_dir: Path) -> CaseResult:
    """plan.json → validate → compile → match expected.sql (and optional rows)."""
    meta, plan = load_case(case_dir)
    catalog = sample_catalog()
    case_id = meta["id"]
    errors: list[str] = []

    if not meta.get("expect_validate_ok", True):
        errs = validate(plan, catalog)
        codes = {e.code for e in errs}
        expected_codes = set(meta.get("expected_error_codes", []))
        if not expected_codes and meta.get("checks", {}).get("expect_error_code"):
            expected_codes = {meta["checks"]["expect_error_code"]}
        if not expected_codes.issubset(codes):
            errors.append(f"expected error codes {sorted(expected_codes)}, got {sorted(codes)}")
        return CaseResult(case_id=case_id, passed=not errors, errors=errors)

    try:
        compiled = validate_and_compile(plan, catalog)
    except Exception as exc:  # noqa: BLE001 — surface in eval report
        return CaseResult(case_id=case_id, passed=False, errors=[str(exc)])

    expected_path = case_dir / "expected.sql"
    if expected_path.exists():
        expected = normalize_sql(expected_path.read_text())
        actual = normalize_sql(compiled.sql)
        if actual != expected:
            errors.append(f"SQL mismatch:\n  expected: {expected}\n  actual:   {actual}")

    # Optional DuckDB row check when sample DB is present
    prefix = meta.get("expected_rows_prefix")
    if prefix is not None:
        from secure_query.examples.load_sample_db import DUCKDB_PATH

        if DUCKDB_PATH.exists():
            import duckdb

            con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
            try:
                rows = con.execute(compiled.sql).fetchall()
            finally:
                con.close()
            for i, expected_row in enumerate(prefix):
                if i >= len(rows):
                    errors.append(f"missing result row {i}")
                    break
                actual_row = list(rows[i])
                if not _rows_close(actual_row, expected_row):
                    errors.append(
                        f"row {i} mismatch: expected {expected_row!r}, got {actual_row!r}"
                    )

    return CaseResult(
        case_id=case_id,
        passed=not errors,
        errors=errors,
        sql=compiled.sql,
    )


def _rows_close(actual: list[Any], expected: list[Any], tol: float = 1e-6) -> bool:
    if len(actual) != len(expected):
        return False
    for a, e in zip(actual, expected, strict=True):
        if isinstance(e, float) or isinstance(a, float):
            try:
                if abs(float(a) - float(e)) > tol:
                    return False
            except (TypeError, ValueError):
                return False
        elif a != e:
            return False
    return True


def score_live_plan(meta: dict[str, Any], plan: LogicalPlan) -> list[str]:
    """Structural checks for live LLM plans (not exact plan_id/SQL)."""
    errors: list[str] = []
    checks = meta.get("checks") or {}
    if "source" in checks and plan.source != checks["source"]:
        errors.append(f"source: expected {checks['source']!r}, got {plan.source!r}")
    if checks.get("must_join"):
        rights = {j.right_table for j in plan.joins}
        if checks["must_join"] not in rights:
            errors.append(f"missing join to {checks['must_join']!r}")
    if "agg_fn" in checks:
        fns = {a.fn for a in plan.aggregations}
        if checks["agg_fn"] not in fns:
            errors.append(f"agg_fn: expected {checks['agg_fn']!r} in {sorted(fns)}")
    if "group_column" in checks and plan.group_by is not None:
        cols = {c.column_id for c in plan.group_by.columns}
        if checks["group_column"] not in cols:
            errors.append(
                f"group_column: expected {checks['group_column']!r} in {sorted(cols)}"
            )
    if "filter_column" in checks:
        cols = {f.column.column_id for f in plan.filters}
        if checks["filter_column"] not in cols:
            errors.append(
                f"filter_column: expected {checks['filter_column']!r} in {sorted(cols)}"
            )
    return errors
