"""Approved metrics — named business measures the planner selects instead of inventing aggs.

Ratio metrics compile via whitelisted sqlglot AST builders (not LLM SQL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

if TYPE_CHECKING:
    from secure_query.catalog import Catalog

from secure_query.logical_plan import (
    Aggregation,
    ColumnRef,
    GroupBy,
    LogicalPlan,
)

MetricKind = Literal["plan", "ratio"]


class MetricSpec(BaseModel):
    """One approved metric definition owned by analysts, not the LLM."""

    id: str
    description: str
    kind: MetricKind = "plan"
    # For kind=plan: template fields used by expand_metric_plan
    source: str | None = None
    group_by: list[str] = Field(default_factory=list)  # "Table.Column"
    aggregations: list[str] = Field(default_factory=list)  # "sum:Invoice.Total:revenue"
    joins: list[str] = Field(default_factory=list)  # "Invoice:Customer:Invoice.CustomerId=Customer.CustomerId"
    default_limit: int = 10
    # For kind=ratio: registered compile id
    ratio_id: str | None = None
    tables: list[str] = Field(
        default_factory=list,
        description="Tables this metric reads; required for ratio metrics, inferred for plan metrics",
    )

    model_config = ConfigDict(extra="forbid", frozen=True)


def _parse_col(ref: str) -> ColumnRef:
    table_id, column_id = ref.split(".", 1)
    return ColumnRef(table_id=table_id, column_id=column_id)


def _parse_agg(spec: str) -> Aggregation:
    """Parse 'fn:Table.Column:alias' or 'count:*:alias'."""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"bad aggregation spec: {spec!r}")
    fn, col, alias = parts
    column = None if col == "*" else _parse_col(col)
    return Aggregation(fn=fn, column=column, alias=alias)  # type: ignore[arg-type]


def expand_metric_plan(metric: MetricSpec, *, limit: int | None = None) -> LogicalPlan:
    """Expand a plan-kind metric to a LogicalPlan."""
    from uuid import uuid4

    from secure_query.logical_plan import Join, JoinCondition

    if metric.kind != "plan" or not metric.source:
        raise ValueError(f"metric {metric.id!r} is not a plan metric")

    joins: list[Join] = []
    for j in metric.joins:
        left_table, right_table, cond = j.split(":", 2)
        lc, rc = cond.split("=")
        joins.append(
            Join(
                right_table=right_table,
                kind="inner",
                conditions=[JoinCondition(left=_parse_col(lc), right=_parse_col(rc))],
            )
        )

    return LogicalPlan(
        plan_id=uuid4(),
        source=metric.source,
        joins=joins,
        group_by=GroupBy(columns=[_parse_col(c) for c in metric.group_by]) if metric.group_by else None,
        aggregations=[_parse_agg(a) for a in metric.aggregations],
        limit=limit or metric.default_limit,
    )


# Whitelisted ratio metrics — AST only, no string SQL from LLM.
_RATIO_BUILDERS: dict[str, Callable[[], exp.Select]] = {}


def register_ratio(id: str, builder: Callable[[], exp.Select]) -> None:
    _RATIO_BUILDERS[id] = builder


def compile_ratio_metric(ratio_id: str, *, dialect: str = "duckdb") -> str:
    if ratio_id not in _RATIO_BUILDERS:
        raise ValueError(f"unknown ratio metric: {ratio_id!r}")
    return _RATIO_BUILDERS[ratio_id]().sql(dialect=dialect, pretty=False)


def metric_tables(metric: MetricSpec) -> frozenset[str]:
    """Every table a metric reads. Used to filter metrics onto a principal catalog."""
    tables: set[str] = set(metric.tables)
    if metric.source:
        tables.add(metric.source)
    for join in metric.joins:
        left_table, right_table, _cond = join.split(":", 2)
        tables.add(left_table)
        tables.add(right_table)
    for ref in metric.group_by:
        tables.add(ref.split(".", 1)[0])
    for spec in metric.aggregations:
        parts = spec.split(":")
        if len(parts) == 3 and parts[1] != "*":
            tables.add(parts[1].split(".", 1)[0])
    return frozenset(tables)


def assert_metric_authorized(metric: MetricSpec, catalog: Catalog) -> None:
    """Refuse metrics whose tables are not all in the (already sliced) catalog."""
    visible = set(catalog.table_map())
    missing = sorted(metric_tables(metric) - visible)
    if missing:
        raise PermissionError(
            f"metric {metric.id!r} requires tables not in this catalog: {missing}"
        )


def _col(table: str, column: str) -> exp.Column:
    return exp.Column(
        this=exp.to_identifier(column, quoted=True),
        table=exp.to_identifier(table, quoted=True),
    )


def _register_builtin_ratios() -> None:
    def avg_revenue_per_customer() -> exp.Select:
        total = exp.Anonymous(this="SUM", expressions=[_col("Invoice", "Total")])
        distinct_customers = exp.Count(
            this=exp.Distinct(expressions=[_col("Invoice", "CustomerId")])
        )
        ratio = exp.Div(this=total, expression=distinct_customers)
        return (
            exp.Select()
            .select(
                exp.Alias(
                    this=ratio,
                    alias=exp.to_identifier("avg_revenue_per_customer", quoted=True),
                )
            )
            .from_(exp.Table(this=exp.to_identifier("Invoice", quoted=True)))
        )

    register_ratio("avg_revenue_per_customer", avg_revenue_per_customer)


_register_builtin_ratios()


def metrics_for_catalog(catalog: Catalog) -> list[MetricSpec]:
    """Return MetricSpec entries listed on the catalog."""
    from secure_query.catalog import Catalog as CatalogType

    assert isinstance(catalog, CatalogType)
    registry = {m.id: m for m in chinook_metrics()}
    return [registry[mid] for mid in catalog.metric_ids if mid in registry]


def get_metric(metric_id: str, catalog: Catalog) -> MetricSpec | None:
    return next((m for m in metrics_for_catalog(catalog) if m.id == metric_id), None)


def chinook_metrics() -> list[MetricSpec]:
    """Default Chinook metric registry."""
    return [
        MetricSpec(
            id="total_revenue",
            description="Sum of all invoice totals",
            source="Invoice",
            aggregations=["sum:Invoice.Total:total_revenue"],
            default_limit=1,
        ),
        MetricSpec(
            id="invoice_count",
            description="Count of invoices",
            source="Invoice",
            aggregations=["count:*:invoice_count"],
            default_limit=1,
        ),
        MetricSpec(
            id="employee_count",
            description="Count of employees (staff headcount)",
            source="Employee",
            aggregations=["count:*:employee_count"],
            default_limit=1,
        ),
        MetricSpec(
            id="revenue_by_country",
            description="Total invoice revenue grouped by customer country",
            source="Invoice",
            joins=["Invoice:Customer:Invoice.CustomerId=Customer.CustomerId"],
            group_by=["Customer.Country"],
            aggregations=["sum:Invoice.Total:revenue"],
            default_limit=24,
        ),
        MetricSpec(
            id="revenue_by_billing_country",
            description="Total invoice revenue grouped by billing country on invoice",
            source="Invoice",
            group_by=["Invoice.BillingCountry"],
            aggregations=["sum:Invoice.Total:revenue"],
            default_limit=24,
        ),
        MetricSpec(
            id="revenue_by_genre",
            description="Track sales revenue grouped by genre name",
            source="InvoiceLine",
            joins=[
                "InvoiceLine:Track:InvoiceLine.TrackId=Track.TrackId",
                "Track:Genre:Track.GenreId=Genre.GenreId",
            ],
            group_by=["Genre.Name"],
            aggregations=["sum:InvoiceLine.UnitPrice:revenue"],
            default_limit=24,
        ),
        MetricSpec(
            id="avg_revenue_per_customer",
            description="Total revenue divided by distinct customers (ratio metric)",
            kind="ratio",
            ratio_id="avg_revenue_per_customer",
            tables=["Invoice"],
            default_limit=1,
        ),
    ]
