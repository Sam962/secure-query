"""Render a LogicalPlan back into plain English.

Validation proves a plan is *safe*; it cannot prove the plan answers the
question that was asked. A plan that averages the wrong column, groups at the
wrong grain, or silently drops a filter is just as valid as a correct one, and
the compiled SQL is not something most users will read.

This module closes that gap by describing the plan in words the person who
asked the question can confirm or reject. It is deterministic — no model call —
so the explanation can never drift from the SQL that actually runs.

The rendering deliberately states the *grain* of every aggregate ("averaged
across Invoice rows") because averaging per-invoice when the user meant
per-customer is the most common silent error.
"""

from __future__ import annotations

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import (
    Aggregation,
    ColumnRef,
    Filter,
    GroupBy,
    LiteralValue,
    LogicalPlan,
    TimeBucket,
)

_COMPARISONS = {
    "eq": "is",
    "ne": "is not",
    "lt": "is less than",
    "lte": "is at most",
    "gt": "is greater than",
    "gte": "is at least",
}

_AGG_PHRASES = {
    "count": "number of",
    "count_distinct": "number of distinct",
    "sum": "sum of",
    "avg": "average of",
    "min": "smallest",
    "max": "largest",
}


def explain_plan(plan: LogicalPlan, catalog: Catalog | None = None) -> str:
    """Describe what `plan` computes, one clause per line.

    Passing `catalog` adds the concrete column list for list intents, so the
    reader can see exactly which fields come back (and that PII ones do not).
    """
    lines = [_describe_source(plan)]

    if plan.filters:
        lines.append("Keeps only rows where " + _join_clauses(plan.filters) + ".")

    for agg in plan.aggregations:
        lines.append(_describe_aggregation(agg, plan))

    lines.append(_describe_grain(plan, catalog))

    if plan.having:
        lines.append("Then keeps only groups where " + _join_clauses(plan.having) + ".")

    if plan.order_by:
        lines.append("Sorted by " + _describe_ordering(plan) + ".")

    if plan.limit is not None:
        lines.append(f"Returns at most {plan.limit} rows.")

    return "\n".join(lines)


def _describe_source(plan: LogicalPlan) -> str:
    text = f"Reads {plan.source}"
    for join in plan.joins:
        if join.kind == "cross":
            text += f", combined with every row of {join.right_table}"
            continue
        conditions = " and ".join(
            f"{_column(c.left)} = {_column(c.right)}" for c in join.conditions
        )
        text += f", matched to {join.right_table} where {conditions}"
    return text + "."


def _describe_aggregation(agg: Aggregation, plan: LogicalPlan) -> str:
    phrase = _AGG_PHRASES[agg.fn]
    if agg.column is None:
        subject = f"rows in {plan.source}"
        grain = ""
    else:
        subject = _column(agg.column)
        # Naming the table the values come from is what exposes a wrong grain,
        # e.g. "averaged across Invoice rows" when the user meant per customer.
        grain = f", computed across {agg.column.table_id} rows"
    return f'Computes the {phrase} {subject}{grain}, reported as "{agg.alias}".'


def _describe_grain(plan: LogicalPlan, catalog: Catalog | None) -> str:
    if plan.group_by is not None and _has_grouping(plan.group_by):
        keys = [_column(col) for col in plan.group_by.columns]
        keys += [_describe_bucket(bucket) for bucket in plan.group_by.time_buckets]
        return "Returns one row per " + _join_words(keys) + "."

    if plan.aggregations:
        return "Returns a single row summarising everything above."

    if catalog is not None:
        from secure_query.kernel.validate import safe_projection

        columns = [col.column_id for col in safe_projection(plan, catalog)]
        if columns:
            return "Returns one row per matching record, with: " + ", ".join(columns) + "."
    return "Returns one row per matching record."


def _describe_bucket(bucket: TimeBucket) -> str:
    return f"{bucket.grain} of {_column(bucket.column)}"


def _describe_ordering(plan: LogicalPlan) -> str:
    parts = []
    for order in plan.order_by:
        target = order.alias if order.alias is not None else _column(order.column)
        direction = "highest first" if order.direction == "desc" else "lowest first"
        parts.append(f"{target} ({direction})")
    return ", then ".join(parts)


def _join_clauses(filters: list[Filter]) -> str:
    return _join_words([_describe_filter(f) for f in filters], conjunction="and")


def _describe_filter(filt: Filter) -> str:
    column = _column(filt.column)
    op = filt.op

    if op in _COMPARISONS:
        return f"{column} {_COMPARISONS[op]} {_value(filt.value)}"
    if op == "in":
        return f"{column} is one of {_join_words([_value(v) for v in filt.values], 'or')}"
    if op == "not_in":
        return f"{column} is none of {_join_words([_value(v) for v in filt.values], 'or')}"
    if op == "between":
        return f"{column} is between {_value(filt.low)} and {_value(filt.high)}"
    if op == "is_null":
        return f"{column} is empty"
    if op == "not_null":
        return f"{column} is not empty"
    if op == "like":
        return f"{column} matches the pattern {_value(filt.pattern)}"
    return f"{column} {op}"


def _value(value: ColumnRef | LiteralValue) -> str:
    if isinstance(value, ColumnRef):
        return _column(value)
    if value.type == "string":
        return f"'{value.value}'"
    return str(value.value)


def _column(ref: ColumnRef | None) -> str:
    if ref is None:
        return "?"
    return f"{ref.table_id}.{ref.column_id}"


def _has_grouping(group_by: GroupBy) -> bool:
    return bool(group_by.columns or group_by.time_buckets)


def _join_words(items: list[str], conjunction: str = "and") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"
