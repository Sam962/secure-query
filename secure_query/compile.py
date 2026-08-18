"""Deterministic LogicalPlan -> DuckDB SQL compiler via sqlglot AST construction.

No string interpolation. Identifiers and literals are sqlglot nodes only.
Change dialect= below if your target is not DuckDB.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlglot import exp

from secure_query.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    Eq,
    Filter,
    GroupBy,
    Gt,
    Gte,
    In,
    IsNull,
    Join,
    Like,
    LiteralValue,
    LogicalPlan,
    Lt,
    Lte,
    NotEq,
    NotIn,
    NotNull,
    OrderBy,
    TimeBucket,
)

# Default compile target. Override in compile() call sites if you fork for Postgres/etc.
DEFAULT_DIALECT = "duckdb"


class CompilationError(Exception):
    """Raised when the compiler encounters an LQP node it cannot handle."""

    def __init__(self, message: str, node_path: str = "$") -> None:
        self.node_path = node_path
        super().__init__(f"[{node_path}] {message}")


@dataclass(frozen=True)
class CompiledQuery:
    """Output of the compiler: SQL text plus integrity hashes for audit/replay."""

    sql: str
    plan_hash: str
    sql_hash: str
    parameters: list[object]


def compile(
    plan: LogicalPlan,
    *,
    dialect: str = DEFAULT_DIALECT,
    projection: list[ColumnRef] | None = None,
) -> CompiledQuery:
    """Compile a LogicalPlan to dialect SQL via sqlglot AST.

    Returns a CompiledQuery with the SQL text and integrity hashes.
    Raises CompilationError on unhandled nodes or invalid structure.

    `projection` names the columns a list intent may return. Without it the
    compiler falls back to `SELECT *`, which cannot honour column-level policy —
    so production callers should use validate_and_compile, which supplies it.
    """
    select = _build_select(plan, projection)
    sql = select.sql(dialect=dialect, pretty=False)

    plan_hash = _hash_plan(plan)
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()

    return CompiledQuery(
        sql=sql,
        plan_hash=plan_hash,
        sql_hash=sql_hash,
        parameters=[],
    )


def _build_select(
    plan: LogicalPlan, projection: list[ColumnRef] | None = None
) -> exp.Select:
    """Build the full SELECT expression from a LogicalPlan."""
    select = exp.Select()

    select = select.from_(exp.Table(this=exp.to_identifier(plan.source, quoted=True)))

    for join in plan.joins:
        select = _apply_join(select, join)

    for filt in plan.filters:
        select = select.where(_compile_filter(filt))

    if plan.group_by:
        select = _apply_group_by(select, plan.group_by, plan.aggregations)
    elif plan.aggregations:
        for agg in plan.aggregations:
            select = select.select(_compile_aggregation(agg))
    elif projection:
        for col in projection:
            select = select.select(_compile_column_ref(col))
    else:
        select = select.select("*")

    for having_filt in plan.having:
        select = select.having(_compile_filter(having_filt))

    for ob in plan.order_by:
        select = _apply_order_by(select, ob)

    if plan.limit is not None:
        select = select.limit(plan.limit)

    return select


def _apply_join(select: exp.Select, join: Join) -> exp.Select:
    """Add a JOIN clause to the SELECT."""
    if join.kind == "cross" or not join.conditions:
        raise CompilationError(
            "Cross joins are not allowed; join conditions are required",
            "$.joins",
        )
    right_table = exp.Table(this=exp.to_identifier(join.right_table, quoted=True))

    on_clause: exp.Expression | None = None
    for cond in join.conditions:
        eq = exp.EQ(
            this=_compile_column_ref(cond.left),
            expression=_compile_column_ref(cond.right),
        )
        if on_clause is None:
            on_clause = eq
        else:
            on_clause = exp.And(this=on_clause, expression=eq)

    return select.join(right_table, on=on_clause, join_type=join.kind.upper())


def _apply_group_by(
    select: exp.Select,
    group_by: GroupBy,
    aggregations: list[Aggregation],
) -> exp.Select:
    """Apply GROUP BY columns/time_buckets and select group keys + aggregates."""
    group_exprs: list[exp.Expression] = []

    for col in group_by.columns:
        col_expr = _compile_column_ref(col)
        select = select.select(col_expr.copy())
        group_exprs.append(col_expr)

    for tb in group_by.time_buckets:
        tb_expr = _compile_time_bucket(tb)
        select = select.select(
            exp.Alias(
                this=tb_expr,
                alias=exp.to_identifier(f"{tb.column.column_id}_{tb.grain}", quoted=True),
            )
        )
        group_exprs.append(tb_expr.copy())

    for agg in aggregations:
        select = select.select(_compile_aggregation(agg))

    for ge in group_exprs:
        select = select.group_by(ge.copy(), append=True)

    return select


def _apply_order_by(select: exp.Select, ob: OrderBy) -> exp.Select:
    """Add an ORDER BY clause."""
    desc = ob.direction == "desc"

    if ob.column is not None:
        order_expr = _compile_column_ref(ob.column)
    elif ob.alias is not None:
        order_expr = exp.Column(this=exp.to_identifier(ob.alias, quoted=True))
    else:
        raise CompilationError("OrderBy has neither column nor alias", "$.order_by")

    ordered = exp.Ordered(this=order_expr, desc=desc)
    return select.order_by(ordered, append=True)


def _compile_column_ref(col: ColumnRef) -> exp.Column:
    """Compile a ColumnRef to a qualified sqlglot Column."""
    return exp.Column(
        this=exp.to_identifier(col.column_id, quoted=True),
        table=exp.to_identifier(col.table_id, quoted=True),
    )


def _compile_literal(lit: LiteralValue) -> exp.Expression:
    """Compile a LiteralValue to a typed sqlglot Literal."""
    if lit.type == "string":
        return exp.Literal.string(str(lit.value))
    if lit.type == "integer":
        return exp.Literal.number(int(lit.value))
    if lit.type == "float":
        return exp.Literal.number(float(lit.value))
    if lit.type == "boolean":
        return exp.Boolean(this=bool(lit.value))
    if lit.type == "date":
        val = lit.value
        if isinstance(val, date):
            return exp.Cast(
                this=exp.Literal.string(val.isoformat()),
                to=exp.DataType(this=exp.DataType.Type.DATE),
            )
        return exp.Literal.string(str(val))
    if lit.type == "datetime":
        val = lit.value
        if isinstance(val, datetime):
            return exp.Cast(
                this=exp.Literal.string(val.isoformat()),
                to=exp.DataType(this=exp.DataType.Type.TIMESTAMP),
            )
        return exp.Literal.string(str(val))
    raise CompilationError(f"Unknown literal type: {lit.type}", "$.literal")


def _compile_value(val: ColumnRef | LiteralValue) -> exp.Expression:
    """Compile a filter value that can be either a ColumnRef or LiteralValue."""
    if isinstance(val, ColumnRef):
        return _compile_column_ref(val)
    return _compile_literal(val)


def _compile_filter(filt: Filter) -> exp.Expression:
    """Compile a Filter predicate to a sqlglot expression."""
    left = _compile_column_ref(filt.column)

    if isinstance(filt, Eq):
        return exp.EQ(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, NotEq):
        return exp.NEQ(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, Lt):
        return exp.LT(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, Lte):
        return exp.LTE(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, Gt):
        return exp.GT(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, Gte):
        return exp.GTE(this=left, expression=_compile_value(filt.value))
    if isinstance(filt, In):
        values = [_compile_literal(v) for v in filt.values]
        return exp.In(this=left, expressions=values)
    if isinstance(filt, NotIn):
        values = [_compile_literal(v) for v in filt.values]
        return exp.Not(this=exp.In(this=left, expressions=values))
    if isinstance(filt, Between):
        return exp.Between(
            this=left,
            low=_compile_literal(filt.low),
            high=_compile_literal(filt.high),
        )
    if isinstance(filt, IsNull):
        return exp.Is(this=left, expression=exp.Null())
    if isinstance(filt, NotNull):
        return exp.Not(this=exp.Is(this=left, expression=exp.Null()))
    if isinstance(filt, Like):
        return exp.Like(this=left, expression=_compile_literal(filt.pattern))
    raise CompilationError(f"Unknown filter op: {getattr(filt, 'op', '?')}", "$.filters")


def _compile_aggregation(agg: Aggregation) -> exp.Expression:
    """Compile an Aggregation to a sqlglot aggregate function call."""
    fn_map = {
        "count": "COUNT",
        "count_distinct": "COUNT",
        "sum": "SUM",
        "avg": "AVG",
        "min": "MIN",
        "max": "MAX",
    }

    fn_name = fn_map.get(agg.fn)
    if fn_name is None:
        raise CompilationError(f"Unknown aggregate function: {agg.fn}", "$.aggregations")

    if agg.fn == "count" and agg.column is None:
        inner: exp.Expression = exp.Star()
    elif agg.column is not None:
        inner = _compile_column_ref(agg.column)
    else:
        raise CompilationError(
            f"Aggregate '{agg.fn}' requires a column (only count allows None)",
            "$.aggregations",
        )

    if agg.fn == "count_distinct":
        agg_expr: exp.Expression = exp.Count(this=exp.Distinct(expressions=[inner]))
    else:
        agg_expr = exp.Anonymous(this=fn_name, expressions=[inner])

    return exp.Alias(
        this=agg_expr,
        alias=exp.to_identifier(agg.alias, quoted=True),
    )


def _compile_time_bucket(tb: TimeBucket) -> exp.Expression:
    """Compile a TimeBucket to a DATE_TRUNC expression."""
    col_expr = _compile_column_ref(tb.column)
    grain_literal = exp.Literal.string(tb.grain)
    return exp.Anonymous(this="DATE_TRUNC", expressions=[grain_literal, col_expr])


def _hash_plan(plan: LogicalPlan) -> str:
    """Compute a stable SHA-256 hash of the plan's canonical JSON form."""
    canonical = json.dumps(
        json.loads(plan.model_dump_json()),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
