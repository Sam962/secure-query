"""Validate a LogicalPlan against an approved Catalog before compile.

This is the trust gate: unknown tables/columns, bad types, disallowed joins,
and missing limits are rejected here — never at SQL string time.
"""

from __future__ import annotations

from secure_query.catalog import Catalog, ColumnSpec
from secure_query.compile import CompiledQuery, CompilationError, compile
from secure_query.errors import ValidationError
from secure_query.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    Eq,
    Filter,
    Gt,
    Gte,
    In,
    Like,
    LiteralValue,
    LogicalPlan,
    Lt,
    Lte,
    NotEq,
    NotIn,
)


class PlanValidationFailed(Exception):
    """Raised by validate_and_compile when the plan fails catalog checks."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        msgs = "; ".join(f"{e.code}: {e.message}" for e in errors)
        super().__init__(msgs)


_LITERAL_TO_DTYPE = {
    "string": {"str"},
    "integer": {"int"},
    "float": {"float", "int"},
    "boolean": {"bool"},
    "date": {"datetime"},
    "datetime": {"datetime"},
}

_NUMERIC_AGGS = {"sum", "avg"}


def validate(plan: LogicalPlan, catalog: Catalog) -> list[ValidationError]:
    """Return all validation errors (empty list = ok to compile)."""
    errors: list[ValidationError] = []

    if not catalog.has_table(plan.source):
        errors.append(
            ValidationError(
                code="catalog.unknown_table",
                path="$.source",
                message=f"Unknown source table: {plan.source!r}",
                stage="typecheck",
            )
        )

    for i, join in enumerate(plan.joins):
        path = f"$.joins[{i}]"
        if not catalog.has_table(join.right_table):
            errors.append(
                ValidationError(
                    code="catalog.unknown_table",
                    path=f"{path}.right_table",
                    message=f"Unknown join table: {join.right_table!r}",
                    stage="typecheck",
                )
            )
        for j, cond in enumerate(join.conditions):
            errors.extend(_check_column_ref(cond.left, catalog, f"{path}.conditions[{j}].left"))
            errors.extend(_check_column_ref(cond.right, catalog, f"{path}.conditions[{j}].right"))
            if catalog.has_table(cond.left.table_id) and catalog.has_table(cond.right.table_id):
                if not catalog.join_allowed(
                    cond.left.table_id,
                    cond.left.column_id,
                    cond.right.table_id,
                    cond.right.column_id,
                ):
                    errors.append(
                        ValidationError(
                            code="policy.join_not_allowed",
                            path=f"{path}.conditions[{j}]",
                            message=(
                                f"Join not in approved join_keys: "
                                f"{cond.left.table_id}.{cond.left.column_id} = "
                                f"{cond.right.table_id}.{cond.right.column_id}"
                            ),
                            stage="policy",
                        )
                    )

    for i, filt in enumerate(plan.filters):
        errors.extend(_check_filter(filt, catalog, f"$.filters[{i}]"))

    for i, filt in enumerate(plan.having):
        errors.extend(_check_filter(filt, catalog, f"$.having[{i}]"))

    if plan.group_by is not None:
        for i, col in enumerate(plan.group_by.columns):
            errors.extend(_check_column_ref(col, catalog, f"$.group_by.columns[{i}]"))
        for i, tb in enumerate(plan.group_by.time_buckets):
            errors.extend(
                _check_column_ref(tb.column, catalog, f"$.group_by.time_buckets[{i}].column")
            )

    for i, agg in enumerate(plan.aggregations):
        errors.extend(_check_aggregation(agg, catalog, f"$.aggregations[{i}]"))

    for i, ob in enumerate(plan.order_by):
        if ob.column is not None:
            errors.extend(_check_column_ref(ob.column, catalog, f"$.order_by[{i}].column"))

    if catalog.require_limit and plan.limit is None:
        errors.append(
            ValidationError(
                code="policy.limit_required",
                path="$.limit",
                message="limit is required by catalog policy",
                stage="policy",
            )
        )
    if plan.limit is not None and plan.limit > catalog.max_limit:
        errors.append(
            ValidationError(
                code="policy.limit_too_large",
                path="$.limit",
                message=f"limit {plan.limit} exceeds catalog.max_limit={catalog.max_limit}",
                stage="policy",
            )
        )

    # Raw list intents: block high-PII columns appearing in filters only as soft signal;
    # SELECT * is still a product risk — prefer explicit projections in a fork.
    if not plan.aggregations and not plan.group_by:
        for i, filt in enumerate(plan.filters):
            col = catalog.get_column(filt.column.table_id, filt.column.column_id)
            if col is not None and col.pii_risk == "high":
                errors.append(
                    ValidationError(
                        code="policy.pii_filter_blocked",
                        path=f"$.filters[{i}]",
                        message=(
                            f"High-PII column {filt.column.table_id}.{filt.column.column_id} "
                            "cannot be used in list/filter queries"
                        ),
                        stage="policy",
                    )
                )

    return errors


def validate_and_compile(plan: LogicalPlan, catalog: Catalog) -> CompiledQuery:
    """Validate against catalog, then compile. Raises PlanValidationFailed or CompilationError."""
    errors = validate(plan, catalog)
    if errors:
        raise PlanValidationFailed(errors)
    try:
        return compile(plan)
    except CompilationError:
        raise


def _check_column_ref(
    ref: ColumnRef, catalog: Catalog, path: str
) -> list[ValidationError]:
    if not catalog.has_table(ref.table_id):
        return [
            ValidationError(
                code="catalog.unknown_table",
                path=path,
                message=f"Unknown table: {ref.table_id!r}",
                stage="typecheck",
            )
        ]
    if catalog.get_column(ref.table_id, ref.column_id) is None:
        return [
            ValidationError(
                code="catalog.unknown_column",
                path=path,
                message=f"Unknown column: {ref.table_id}.{ref.column_id}",
                stage="typecheck",
            )
        ]
    return []


def _check_filter(filt: Filter, catalog: Catalog, path: str) -> list[ValidationError]:
    errors = _check_column_ref(filt.column, catalog, f"{path}.column")
    col = catalog.get_column(filt.column.table_id, filt.column.column_id)

    if isinstance(filt, (Eq, NotEq, Lt, Lte, Gt, Gte)):
        errors.extend(_check_value(filt.value, col, catalog, f"{path}.value"))
    elif isinstance(filt, (In, NotIn)):
        for i, lit in enumerate(filt.values):
            errors.extend(_check_literal_type(lit, col, f"{path}.values[{i}]"))
    elif isinstance(filt, Between):
        errors.extend(_check_literal_type(filt.low, col, f"{path}.low"))
        errors.extend(_check_literal_type(filt.high, col, f"{path}.high"))
    elif isinstance(filt, Like):
        errors.extend(_check_literal_type(filt.pattern, col, f"{path}.pattern"))
        if col is not None and col.dtype != "str":
            errors.append(
                ValidationError(
                    code="typecheck.like_on_non_string",
                    path=path,
                    message=f"LIKE requires string column, got {col.dtype}",
                    stage="typecheck",
                )
            )
    return errors


def _check_value(
    value: ColumnRef | LiteralValue,
    col: ColumnSpec | None,
    catalog: Catalog,
    path: str,
) -> list[ValidationError]:
    if isinstance(value, ColumnRef):
        return _check_column_ref(value, catalog, path)
    return _check_literal_type(value, col, path)


def _check_literal_type(
    lit: LiteralValue, col: ColumnSpec | None, path: str
) -> list[ValidationError]:
    if col is None:
        return []
    allowed = _LITERAL_TO_DTYPE.get(lit.type, set())
    if col.dtype not in allowed:
        return [
            ValidationError(
                code="typecheck.literal_mismatch",
                path=path,
                message=(
                    f"Literal type {lit.type!r} incompatible with column dtype {col.dtype!r}"
                ),
                stage="typecheck",
            )
        ]
    return []


def _check_aggregation(
    agg: Aggregation, catalog: Catalog, path: str
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if agg.column is None:
        if agg.fn != "count":
            errors.append(
                ValidationError(
                    code="typecheck.agg_requires_column",
                    path=path,
                    message=f"Aggregate {agg.fn!r} requires a column",
                    stage="typecheck",
                )
            )
        return errors

    errors.extend(_check_column_ref(agg.column, catalog, f"{path}.column"))
    col = catalog.get_column(agg.column.table_id, agg.column.column_id)
    if col is not None and agg.fn in _NUMERIC_AGGS and not col.numeric():
        errors.append(
            ValidationError(
                code="typecheck.agg_non_numeric",
                path=path,
                message=f"{agg.fn} on non-numeric column {agg.column.column_id}",
                stage="typecheck",
            )
        )
    return errors
