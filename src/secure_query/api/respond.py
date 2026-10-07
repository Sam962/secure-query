"""Template answers without sending result cells to an LLM."""

from __future__ import annotations

import math
from decimal import Decimal
from numbers import Integral, Real
from typing import Any, Sequence

from secure_query.engine.execute import ExecutionResult
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan

# Binary float dust around invoice money (523.0600000000003).
_CENT_REL_TOL = 1e-8
_VALUE_AGGS = frozenset({"sum", "avg", "min", "max"})


def format_result_value(value: Any) -> Any:
    """Clean a single cell for API/CLI display. Does not change executed SQL."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Decimal):
        return _clean_float(float(value))
    if isinstance(value, Real):
        return _clean_float(float(value))
    return value


def format_result_cell(value: Any, *, unit: str | None = None) -> str:
    """Plain-text cell: USD as $523.06, other money-like floats as two decimals."""
    cleaned = format_result_value(value)
    if unit == "USD" and isinstance(cleaned, (int, float)) and not isinstance(cleaned, bool):
        return _format_usd(float(cleaned))
    if isinstance(cleaned, float):
        cents = round(cleaned, 2)
        if math.isfinite(cleaned) and abs(cleaned - cents) < 1e-9:
            return f"{cents:.2f}"
        text = f"{cleaned:.10f}".rstrip("0").rstrip(".")
        return text
    return str(cleaned)


def result_column_units(
    columns: Sequence[str],
    *,
    plan: LogicalPlan | None,
    catalog: Catalog | None,
    compiled: CompiledQuery | None = None,
) -> list[str | None]:
    """Catalog unit per result column. None when the column is not a measured amount."""
    units: list[str | None] = [None] * len(columns)
    if catalog is None:
        return units
    by_name: dict[str, str] = {}

    if compiled is not None and compiled.plan_hash.startswith("metric:"):
        from secure_query.kernel.metrics import get_metric

        metric = get_metric(compiled.plan_hash.split(":")[1], catalog)
        if metric is not None and metric.unit:
            for i, name in enumerate(columns):
                if name.lower() not in {"country", "name", "title"}:
                    units[i] = metric.unit
            if any(units):
                return units

    if plan is not None:
        for agg in plan.aggregations:
            if agg.fn not in _VALUE_AGGS or agg.column is None:
                continue
            spec = catalog.get_column(agg.column.table_id, agg.column.column_id)
            if spec is not None and spec.unit:
                by_name[agg.alias] = spec.unit
        for col in _plan_output_columns(plan):
            spec = catalog.get_column(col.table_id, col.column_id)
            if spec is not None and spec.unit:
                by_name[col.column_id] = spec.unit
                by_name[f"{col.table_id}.{col.column_id}"] = spec.unit

    for i, name in enumerate(columns):
        units[i] = by_name.get(name)
    return units


def money_scale_note(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    units: Sequence[str | None],
) -> str | None:
    """One line stating USD is unscaled and which magnitude band the values fall in."""
    _ = columns
    peak = 0.0
    has_usd = False
    for row in rows:
        for i, unit in enumerate(units):
            if unit != "USD" or i >= len(row):
                continue
            has_usd = True
            value = row[i]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            peak = max(peak, abs(float(value)))
    if not has_usd:
        return None
    band = _scale_band(peak)
    return (
        "Amounts are US dollars (USD), unscaled — not in thousands or millions. "
        f"The largest value is in the {band} of dollars."
    )


def format_template_answer(
    question: str,
    plan: LogicalPlan | None,
    result: ExecutionResult,
    *,
    catalog: Catalog | None = None,
    compiled: CompiledQuery | None = None,
    max_rows_show: int = 10,
) -> str:
    """Deterministic answer: explain-back + small result table. No LLM."""
    units = result_column_units(
        result.columns, plan=plan, catalog=catalog, compiled=compiled
    )
    lines = [f"Question: {question}"]
    if plan is not None:
        lines.append(f"Plan: {explain_plan(plan)}")
    lines.append(f"Rows returned: {len(result.rows)}" + (" (truncated)" if result.truncated else ""))
    note = money_scale_note(result.columns, result.rows, units)
    if note:
        lines.append(note)
    if not result.rows:
        lines.append("No matching rows.")
        return "\n".join(lines)
    headers = [
        f"{name} ({unit})" if unit else name
        for name, unit in zip(result.columns, units, strict=True)
    ]
    header = " | ".join(headers)
    lines.append(header)
    lines.append("-" * min(len(header), 80))
    for row in result.rows[:max_rows_show]:
        lines.append(
            " | ".join(
                format_result_cell(c, unit=u)
                for c, u in zip(row, units, strict=False)
            )
        )
    if len(result.rows) > max_rows_show:
        lines.append(f"... and {len(result.rows) - max_rows_show} more rows")
    return "\n".join(lines)


def _plan_output_columns(plan: LogicalPlan):
    cols = []
    if plan.group_by is not None:
        cols.extend(plan.group_by.columns)
    for agg in plan.aggregations:
        if agg.column is not None:
            cols.append(agg.column)
    return cols


def _scale_band(peak: float) -> str:
    if peak >= 1_000_000_000:
        return "billions"
    if peak >= 1_000_000:
        return "millions"
    if peak >= 1_000:
        return "thousands"
    if peak >= 100:
        return "hundreds"
    if peak >= 10:
        return "tens"
    return "ones"


def _format_usd(value: float) -> str:
    cleaned = format_result_value(value)
    number = float(cleaned) if isinstance(cleaned, (int, float)) else value
    if not math.isfinite(number):
        return str(number)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _clean_float(value: float) -> float | int:
    if not math.isfinite(value):
        return value
    cents = round(value, 2)
    if abs(value - cents) < _CENT_REL_TOL * max(1.0, abs(value)):
        if cents == int(cents):
            return int(cents)
        return cents
    cleaned = round(value, 10)
    if cleaned == int(cleaned):
        return int(cleaned)
    return cleaned
