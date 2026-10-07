"""Validate a LogicalPlan against an approved Catalog before compile.

This is the trust gate: unknown tables/columns, bad types, disallowed joins,
and missing limits are rejected here — never at SQL string time.
"""

from __future__ import annotations

import re
from dataclasses import replace

from secure_query.kernel.catalog import Catalog, ColumnSpec
from secure_query.kernel.compile import CompilationError, CompiledQuery, SemiJoin, compile, compile_ratio
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.joins import fan_out_errors, many_side_paths, resolve_joins
from secure_query.kernel.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    Eq,
    Filter,
    GroupBy,
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
from secure_query.kernel.metrics import (
    RATIO_DENOMINATOR_ALIAS,
    RATIO_NUMERATOR_ALIAS,
    MetricSpec,
)


class PlanValidationFailed(Exception):
    """Raised by validate_and_compile when the plan fails catalog checks."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        msgs = "; ".join(f"{e.code}: {e.message}" for e in errors)
        super().__init__(msgs)


_LITERAL_TO_DTYPE = {
    "string": {"str"},
    "integer": {"int", "float"},
    "float": {"float", "int"},
    "boolean": {"bool"},
    "date": {"datetime"},
    "datetime": {"datetime"},
}

_NUMERIC_AGGS = {"sum", "avg"}

# Aggregates that echo a stored cell value back to the caller.
_PII_VALUE_RETURNING_AGGS = {"min", "max"}


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
        if join.kind == "cross" or not join.conditions:
            errors.append(
                ValidationError(
                    code="policy.cross_join_not_allowed",
                    path=path,
                    message=(
                        "Cross joins are not allowed; every join must use an "
                        "approved equi-join from catalog.join_keys"
                    ),
                    stage="policy",
                )
            )
            continue
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

    aliases = {agg.alias for agg in plan.aggregations}
    for i, having in enumerate(plan.having):
        if having.alias not in aliases:
            errors.append(
                ValidationError(
                    code="plan.unknown_having_alias",
                    path=f"$.having[{i}].alias",
                    message=f"HAVING alias {having.alias!r} is not an aggregation in this plan",
                    stage="typecheck",
                )
            )
        if having.value.type not in ("integer", "float"):
            errors.append(
                ValidationError(
                    code="typecheck.literal_mismatch",
                    path=f"$.having[{i}].value",
                    message="HAVING compares an aggregate to a number",
                    stage="typecheck",
                )
            )

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

    errors.extend(_check_pii_policy(plan, catalog))
    errors.extend(_check_label_aggregates(plan, catalog))
    errors.extend(_check_plan_scope(plan))
    errors.extend(_check_dead_order_keys(plan))
    errors.extend(_check_arbitrary_group(plan))
    errors.extend(fan_out_errors(plan, catalog))

    return errors


def _check_dead_order_keys(plan: LogicalPlan) -> list[ValidationError]:
    """Reject sort keys that come after the full group key.

    Aggregate output has one row per group, so once every group_by column has
    been sorted on, later keys can never change the order. A plan like
    ORDER BY Country, revenue DESC LIMIT 5 is a ranking with its keys swapped,
    and the LIMIT keeps the alphabetically first groups instead of the top ones.
    """
    group_by = plan.group_by
    if plan.aggregations:
        # A time-bucketed column is grouped too (ORDER BY compiles to its bucket).
        keys = (
            set(group_by.columns) | {tb.column for tb in group_by.time_buckets}
            if group_by is not None
            else set()
        )
        for i, ob in enumerate(plan.order_by):
            if ob.column is not None and ob.column not in keys:
                return [
                    ValidationError(
                        code="plan.order_key_not_grouped",
                        path=f"$.order_by[{i}].column",
                        message=(
                            f"order_by {ob.column.table_id}.{ob.column.column_id} is not a "
                            "group_by column; sort by an aggregate alias or a grouped column"
                        ),
                        stage="typecheck",
                    )
                ]
    if group_by is None or group_by.time_buckets or not group_by.columns or not plan.aggregations:
        return []
    unsorted = set(group_by.columns)
    for i, ob in enumerate(plan.order_by):
        if not unsorted:
            return [
                ValidationError(
                    code="plan.dead_order_keys",
                    path=f"$.order_by[{i}]",
                    message=(
                        f"order_by[{i}:] can never take effect: the earlier keys already "
                        "sort by every group_by column, which is unique per row. To rank "
                        "by a measure, put its alias first in order_by."
                    ),
                    stage="policy",
                )
            ]
        if ob.column is not None:
            unsorted.discard(ob.column)
    return []


def _check_arbitrary_group(plan: LogicalPlan) -> list[ValidationError]:
    """Reject LIMIT 1 over groups with no ORDER BY: it returns an arbitrary group.

    A larger LIMIT without ORDER BY is fine when it covers every group (a yearly
    trend, a per-shipper total), and the group count is unknown here. One row
    out of several groups is never a meaningful answer without a ranking.
    """
    group_by = plan.group_by
    if group_by is None or not (group_by.columns or group_by.time_buckets):
        return []
    if plan.order_by or plan.limit != 1:
        return []
    return [
        ValidationError(
            code="plan.arbitrary_group",
            path="$.limit",
            message=(
                "limit 1 over grouped rows with no order_by returns an arbitrary group; "
                "order by the measure being ranked, or drop the grouping for a single total"
            ),
            stage="policy",
        )
    ]


def _check_plan_scope(plan: LogicalPlan) -> list[ValidationError]:
    """Reject column refs to tables that are not source or joined.

    Join ON clauses are checked against tables already in FROM at that hop, so
    ``JOIN Track ON InvoiceLine.TrackId = Track.TrackId`` fails unless
    InvoiceLine was joined earlier — otherwise DuckDB raises a binder error.
    """
    reachable = set(plan_tables(plan))

    def check(
        ref: ColumnRef, path: str, tables: set[str] | None = None
    ) -> list[ValidationError]:
        allowed = reachable if tables is None else tables
        if ref.table_id in allowed:
            return []
        return [
            ValidationError(
                code="plan.table_not_in_scope",
                path=path,
                message=(
                    f"Column {ref.table_id}.{ref.column_id} references a table not in "
                    f"the plan; join {ref.table_id!r} before reading its columns"
                ),
                stage="typecheck",
            )
        ]

    errors: list[ValidationError] = []
    in_from = {plan.source}
    for i, join in enumerate(plan.joins):
        in_from.add(join.right_table)
        for j, cond in enumerate(join.conditions):
            errors.extend(
                check(cond.left, f"$.joins[{i}].conditions[{j}].left", in_from)
            )
            errors.extend(
                check(cond.right, f"$.joins[{i}].conditions[{j}].right", in_from)
            )
    for i, filt in enumerate(plan.filters):
        errors.extend(check(filt.column, f"$.filters[{i}].column"))
    if plan.group_by is not None:
        for i, col in enumerate(plan.group_by.columns):
            errors.extend(check(col, f"$.group_by.columns[{i}]"))
        for i, tb in enumerate(plan.group_by.time_buckets):
            errors.extend(check(tb.column, f"$.group_by.time_buckets[{i}].column"))
    for i, agg in enumerate(plan.aggregations):
        if agg.column is not None:
            errors.extend(check(agg.column, f"$.aggregations[{i}].column"))
    for i, ob in enumerate(plan.order_by):
        if ob.column is not None:
            errors.extend(check(ob.column, f"$.order_by[{i}].column"))

    known_aliases = {agg.alias for agg in plan.aggregations}
    for i, ob in enumerate(plan.order_by):
        if ob.alias is not None and ob.alias not in known_aliases:
            errors.append(
                ValidationError(
                    code="plan.unknown_order_alias",
                    path=f"$.order_by[{i}].alias",
                    message=(
                        f"ORDER BY alias {ob.alias!r} is not an aggregation in this plan"
                    ),
                    stage="typecheck",
                )
            )
    return errors


def _check_label_aggregates(plan: LogicalPlan, catalog: Catalog) -> list[ValidationError]:
    """Reject MIN/MAX over a text column in a plan that does not group.

    `MAX(Genre.Name)` alongside `SUM(Quantity)` reads like a per-genre answer but
    returns the alphabetically last genre beside a grand total. Requiring a
    grouping key turns that silent nonsense into a validation error.
    """
    if plan.group_by is not None and (
        plan.group_by.columns or plan.group_by.time_buckets
    ):
        return []

    errors: list[ValidationError] = []
    for i, agg in enumerate(plan.aggregations):
        if agg.fn not in _PII_VALUE_RETURNING_AGGS or agg.column is None:
            continue
        col = catalog.get_column(agg.column.table_id, agg.column.column_id)
        if col is None or col.dtype != "str":
            continue
        errors.append(
            ValidationError(
                code="policy.ungrouped_label_aggregate",
                path=f"$.aggregations[{i}]",
                message=(
                    f"{agg.fn}({agg.column.table_id}.{agg.column.column_id}) picks a value "
                    "alphabetically, not the one matching the other aggregates; "
                    "group by that column instead"
                ),
                stage="policy",
            )
        )
    return errors


def is_list_intent(plan: LogicalPlan) -> bool:
    """True when the plan projects rows rather than aggregates them."""
    return not plan.aggregations and plan.group_by is None


def plan_tables(plan: LogicalPlan) -> list[str]:
    """Source table plus every joined table, in plan order, de-duplicated."""
    tables = [plan.source]
    for join in plan.joins:
        if join.right_table not in tables:
            tables.append(join.right_table)
    return tables


def safe_projection(plan: LogicalPlan, catalog: Catalog) -> list[ColumnRef]:
    """Catalog columns a list intent may emit: everything except high-PII."""
    projection: list[ColumnRef] = []
    table_map = catalog.table_map()
    for table_id in plan_tables(plan):
        table = table_map.get(table_id)
        if table is None:
            continue
        projection.extend(
            ColumnRef(table_id=table_id, column_id=col.name)
            for col in table.columns
            if col.pii_risk != "high"
        )
    return projection


def _alias_impersonates(alias: str, restricted: set[str]) -> bool:
    """True if any word in `alias` names a restricted column.

    Matches on word parts rather than the whole string, because the useful
    disguises are compounds — "email_count", "customer_email", "emails".
    """
    words = {w for w in re.split(r"[^a-z0-9]+", alias.lower()) if w}
    words |= {w[:-1] for w in words if w.endswith("s")}
    return bool(words & restricted)


def _high_pii_names(catalog: Catalog) -> set[str]:
    return {
        col.name.lower()
        for table in catalog.tables
        for col in table.columns
        if col.pii_risk == "high"
    }


def _is_high_pii(catalog: Catalog, ref: ColumnRef) -> bool:
    col = catalog.get_column(ref.table_id, ref.column_id)
    return col is not None and col.pii_risk == "high"


def _pii_error(code: str, path: str, ref: ColumnRef, reason: str) -> ValidationError:
    return ValidationError(
        code=code,
        path=path,
        message=f"High-PII column {ref.table_id}.{ref.column_id} {reason}",
        stage="policy",
    )


def _check_pii_policy(plan: LogicalPlan, catalog: Catalog) -> list[ValidationError]:
    """Keep high-PII columns out of predicates and out of the result set.

    Filtering on a high-PII column is an existence oracle even when the query
    only returns aggregates, so it is blocked for every plan shape — not just
    list intents.
    """
    errors: list[ValidationError] = []

    for label, filters in (("filters", plan.filters),):
        for i, filt in enumerate(filters):
            path = f"$.{label}[{i}]"
            if _is_high_pii(catalog, filt.column):
                errors.append(
                    _pii_error(
                        "policy.pii_filter_blocked",
                        path,
                        filt.column,
                        "cannot be used in filters",
                    )
                )
            value = getattr(filt, "value", None)
            if isinstance(value, ColumnRef) and _is_high_pii(catalog, value):
                errors.append(
                    _pii_error(
                        "policy.pii_filter_blocked",
                        f"{path}.value",
                        value,
                        "cannot be used in filters",
                    )
                )

    if plan.group_by is not None:
        for i, col in enumerate(plan.group_by.columns):
            if _is_high_pii(catalog, col):
                errors.append(
                    _pii_error(
                        "policy.pii_exposed",
                        f"$.group_by.columns[{i}]",
                        col,
                        "cannot be a grouping key (its values are returned)",
                    )
                )
        for i, tb in enumerate(plan.group_by.time_buckets):
            if _is_high_pii(catalog, tb.column):
                errors.append(
                    _pii_error(
                        "policy.pii_exposed",
                        f"$.group_by.time_buckets[{i}].column",
                        tb.column,
                        "cannot be a grouping key (its values are returned)",
                    )
                )

    for i, ob in enumerate(plan.order_by):
        if ob.column is not None and _is_high_pii(catalog, ob.column):
            errors.append(
                _pii_error(
                    "policy.pii_exposed",
                    f"$.order_by[{i}].column",
                    ob.column,
                    "cannot be used for ordering",
                )
            )

    for i, agg in enumerate(plan.aggregations):
        if (
            agg.column is not None
            and agg.fn in _PII_VALUE_RETURNING_AGGS
            and _is_high_pii(catalog, agg.column)
        ):
            errors.append(
                _pii_error(
                    "policy.pii_exposed",
                    f"$.aggregations[{i}]",
                    agg.column,
                    f"cannot be used with {agg.fn} (it returns a stored value)",
                )
            )

    # An alias is free text chosen by the model, so it can dress a substitute
    # answer up as the restricted one it could not compute — e.g. returning
    # COUNT(*) under the name "email". The value is safe; the label is not.
    restricted = _high_pii_names(catalog)
    for i, agg in enumerate(plan.aggregations):
        if _alias_impersonates(agg.alias, restricted):
            errors.append(
                ValidationError(
                    code="policy.misleading_alias",
                    path=f"$.aggregations[{i}].alias",
                    message=(
                        f"Alias {agg.alias!r} names a restricted column; "
                        "results must not be labelled as PII they do not contain"
                    ),
                    stage="policy",
                )
            )

    if (
        is_list_intent(plan)
        and catalog.has_table(plan.source)
        and not safe_projection(plan, catalog)
    ):
        errors.append(
            ValidationError(
                code="policy.no_selectable_columns",
                path="$.source",
                message=(
                    f"Every column of {plan.source!r} is high-PII; "
                    "this plan has nothing it may return"
                ),
                stage="policy",
            )
        )

    return errors


def normalize_plan(plan: LogicalPlan, catalog: Catalog) -> LogicalPlan:
    """Rewrite common planner mistakes using catalog label/display metadata.

    Joins are rebuilt from catalog.join_keys first, so the rewrites below see
    the approved join tree. Raises PlanValidationFailed when no unique approved
    path connects the tables the plan reads.

    Idempotent: safe to call more than once on the same plan.
    """
    plan = _drop_tautological_having(_drop_bucketed_group_columns(plan))
    plan = _rewrite_fk_group_keys(plan, catalog)
    plan, join_errors = resolve_joins(plan, catalog)
    if join_errors:
        raise PlanValidationFailed(join_errors)
    return plan


def _drop_tautological_having(plan: LogicalPlan) -> LogicalPlan:
    """Every group has COUNT >= 1, so `count > 0` / `count >= 1` filters nothing."""
    counts = {a.alias for a in plan.aggregations if a.fn in ("count", "count_distinct") and a.column is None}
    kept = [
        h
        for h in plan.having
        if not (
            h.alias in counts
            and ((h.op == "gt" and h.value.value == 0) or (h.op == "gte" and h.value.value == 1))
        )
    ]
    if len(kept) == len(plan.having):
        return plan
    return plan.model_copy(update={"having": kept})


def _drop_bucketed_group_columns(plan: LogicalPlan) -> LogicalPlan:
    """Grouping by a raw timestamp next to its own bucket defeats the bucket."""
    group_by = plan.group_by
    if group_by is None or not group_by.time_buckets:
        return plan
    bucketed = {tb.column for tb in group_by.time_buckets}
    columns = [c for c in group_by.columns if c not in bucketed]
    if len(columns) == len(group_by.columns):
        return plan
    return plan.model_copy(
        update={"group_by": GroupBy(columns=columns, time_buckets=list(group_by.time_buckets))}
    )


def _rewrite_fk_group_keys(plan: LogicalPlan, catalog: Catalog) -> LogicalPlan:
    """Replace FK grouping keys with their catalog label_for column.

    Joins are resolved afterwards, so the label's table is always reachable
    when the catalog has a path to it.
    """
    if plan.group_by is None or not plan.group_by.columns:
        return plan
    new_cols: list[ColumnRef] = []
    changed = False
    for col in plan.group_by.columns:
        spec = catalog.get_column(col.table_id, col.column_id)
        if spec is not None and spec.label_for:
            label_table, label_col = spec.label_for.split(".", 1)
            label = ColumnRef(table_id=label_table, column_id=label_col)
            if catalog.has_table(label_table) and label != col:
                new_cols.append(label)
                changed = True
                continue
        new_cols.append(col)
    if not changed:
        return plan
    return plan.model_copy(
        update={
            "group_by": GroupBy(
                columns=new_cols,
                time_buckets=list(plan.group_by.time_buckets),
            )
        }
    )


def validate_and_compile(plan: LogicalPlan, catalog: Catalog) -> CompiledQuery:
    """Validate against catalog, then compile. Raises PlanValidationFailed or CompilationError.

    List intents are compiled with an explicit column list drawn from the
    catalog, so high-PII columns never reach the result set via `SELECT *`.
    """
    plan = normalize_plan(plan, catalog)
    errors = validate(plan, catalog)
    semi_joins, semi_tables = _list_semi_joins(plan, catalog, errors)
    if errors:
        raise PlanValidationFailed(errors)
    projection = (
        [c for c in safe_projection(plan, catalog) if c.table_id not in semi_tables]
        if is_list_intent(plan)
        else None
    )
    try:
        return compile(
            plan, dialect=catalog.sql_dialect, projection=projection, semi_joins=semi_joins
        )
    except CompilationError:
        raise


def _list_semi_joins(
    plan: LogicalPlan, catalog: Catalog, errors: list[ValidationError]
) -> tuple[list[SemiJoin], set[str]]:
    """List plans filter many-side tables with EXISTS instead of joining them.

    Without this, "customers with a USA invoice" returns one row per invoice.
    Filters reached through the same first hop share one EXISTS, so conditions
    on one child row stay on the same row. Appends to `errors` when a list is
    sorted by a many-side column or one filter spans two first hops.
    """
    if not is_list_intent(plan) or not plan.joins:
        return [], set()
    paths = many_side_paths(plan, catalog)
    if not paths:
        return [], set()
    for i, ob in enumerate(plan.order_by):
        if ob.column is not None and ob.column.table_id in paths:
            errors.append(
                ValidationError(
                    code="plan.list_order_many_side",
                    path=f"$.order_by[{i}]",
                    message=(
                        f"{ob.column.table_id} has many rows per {plan.source}; a list of "
                        f"{plan.source} cannot be sorted by it. Aggregate instead"
                    ),
                    stage="policy",
                )
            )
    groups: dict[tuple, tuple[list, list[int]]] = {}
    for i, filt in enumerate(plan.filters):
        tables = {filt.column.table_id}
        value = getattr(filt, "value", None)
        if isinstance(value, ColumnRef):
            tables.add(value.table_id)
        hops = {paths[t][0] for t in tables if t in paths}
        if not hops:
            continue
        if len(hops) > 1:
            errors.append(
                ValidationError(
                    code="plan.list_filter_spans_children",
                    path=f"$.filters[{i}]",
                    message="This filter compares two unrelated child tables of a list",
                    stage="policy",
                )
            )
            continue
        steps, indexes = groups.setdefault(hops.pop(), ([], []))
        for t in sorted(tables & set(paths)):
            for step in paths[t]:
                if step not in steps:
                    steps.append(step)
        indexes.append(i)
    semi = [SemiJoin(steps=tuple(st), filter_indexes=tuple(ix)) for st, ix in groups.values()]
    return semi, set(paths)


def validate_and_compile_metric(
    metric: MetricSpec, plan: LogicalPlan, catalog: Catalog
) -> CompiledQuery:
    """Validate and compile a ratio metric's expanded plan (row filters may be injected).

    plan_hash is `metric:<id>:<plan hash>` so answers can look up the metric's unit
    and audit still pins the exact plan, filters included.
    """
    if metric.kind != "ratio":
        raise ValueError(f"metric {metric.id!r} is not a ratio metric")
    plan = normalize_plan(plan, catalog)
    errors = validate(plan, catalog)
    if errors:
        raise PlanValidationFailed(errors)
    compiled = compile_ratio(
        plan,
        numerator_alias=RATIO_NUMERATOR_ALIAS,
        denominator_alias=RATIO_DENOMINATOR_ALIAS,
        alias=metric.id,
        dialect=catalog.sql_dialect,
    )
    return replace(compiled, plan_hash=f"metric:{metric.id}:{compiled.plan_hash}")


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
