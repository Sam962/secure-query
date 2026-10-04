"""Approved metrics — named business measures the planner selects instead of inventing aggs.

Metric definitions are catalog data (`Catalog.metrics`), owned by analysts and
loaded from the approved catalog JSON. Nothing here is domain-specific.

Kinds:
    plan     expands to a LogicalPlan (validated + compiled like any plan)
    ratio    numerator / denominator aggregates over one expanded plan; compiled
             through the validated path, so principal row filters still apply
    builtin  code-registered sqlglot AST builder (escape hatch for expressions the
             IR cannot say, e.g. SUM(a * b)); cannot carry row filters
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

if TYPE_CHECKING:
    from secure_query.kernel.catalog import Catalog

from secure_query.kernel.logical_plan import (
    Aggregation,
    ColumnRef,
    GroupBy,
    Join,
    JoinCondition,
    LogicalPlan,
)

MetricKind = Literal["plan", "ratio", "builtin"]

RATIO_NUMERATOR_ALIAS = "numerator"
RATIO_DENOMINATOR_ALIAS = "denominator"

_METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class MetricSpec(BaseModel):
    """One approved metric definition owned by analysts, not the LLM.

    Spec strings are compact so catalog JSON stays readable:
        column       "Table.Column"
        aggregation  "fn:Table.Column:alias" or "count:*:alias"
        measure      "fn:Table.Column" or "count:*"   (ratio numerator/denominator)
        join         "LeftTable:RightTable:Left.Col=Right.Col"
    """

    id: str
    description: str
    kind: MetricKind = "plan"
    source: str | None = None
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    numerator: str | None = None
    denominator: str | None = None
    builder_id: str | None = Field(
        default=None,
        description="kind=builtin only: id registered with register_builtin()",
    )
    default_limit: int = 10
    tables: list[str] = Field(
        default_factory=list,
        description="Tables this metric reads; required for builtin metrics, inferred otherwise",
    )
    unit: str | None = Field(
        default=None,
        description="Display unit for the metric result (e.g. USD)",
    )
    question: str | None = Field(
        default=None,
        description="Owner-written phrasing offered as a suggestion (askable in this catalog's terms)",
    )

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_shape(self) -> MetricSpec:
        """Reject malformed definitions at catalog load, not at query time."""
        if not _METRIC_ID_RE.match(self.id):
            raise ValueError(f"metric id {self.id!r} must be lower_snake_case")
        if self.kind == "builtin":
            if not self.builder_id:
                raise ValueError(f"builtin metric {self.id!r} requires builder_id")
            if self.builder_id not in _BUILTIN_BUILDERS:
                raise ValueError(f"metric {self.id!r}: unknown builder_id {self.builder_id!r}")
            if not self.tables:
                raise ValueError(f"builtin metric {self.id!r} must list the tables it reads")
            return self
        if self.builder_id is not None:
            raise ValueError(f"metric {self.id!r}: builder_id is only valid for kind=builtin")
        if not self.source:
            raise ValueError(f"{self.kind} metric {self.id!r} requires source")
        for ref in self.group_by:
            _parse_col(ref)
        for spec in self.joins:
            _parse_join(spec)
        if self.kind == "plan":
            if not self.aggregations:
                raise ValueError(f"plan metric {self.id!r} requires aggregations")
            if self.numerator or self.denominator:
                raise ValueError(f"plan metric {self.id!r}: numerator/denominator are ratio-only")
            for spec in self.aggregations:
                _parse_agg(spec)
        else:
            if not (self.numerator and self.denominator):
                raise ValueError(f"ratio metric {self.id!r} requires numerator and denominator")
            if self.aggregations:
                raise ValueError(f"ratio metric {self.id!r}: use numerator/denominator, not aggregations")
            _parse_measure(self.numerator, RATIO_NUMERATOR_ALIAS)
            _parse_measure(self.denominator, RATIO_DENOMINATOR_ALIAS)
        return self


def _parse_col(ref: str) -> ColumnRef:
    table_id, sep, column_id = ref.partition(".")
    if not sep or not table_id or not column_id:
        raise ValueError(f"bad column ref: {ref!r} (expected 'Table.Column')")
    return ColumnRef(table_id=table_id, column_id=column_id)


def _parse_measure(spec: str, alias: str) -> Aggregation:
    """Parse 'fn:Table.Column' or 'count:*'."""
    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(f"bad measure spec: {spec!r} (expected 'fn:Table.Column')")
    fn, col = parts
    column = None if col == "*" else _parse_col(col)
    return Aggregation(fn=fn, column=column, alias=alias)  # type: ignore[arg-type]


def _parse_agg(spec: str) -> Aggregation:
    """Parse 'fn:Table.Column:alias' or 'count:*:alias'."""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"bad aggregation spec: {spec!r}")
    fn, col, alias = parts
    return _parse_measure(f"{fn}:{col}", alias)


def _parse_join(spec: str) -> tuple[str, str, Join]:
    parts = spec.split(":", 2)
    if len(parts) != 3 or "=" not in parts[2]:
        raise ValueError(f"bad join spec: {spec!r} (expected 'Left:Right:Left.Col=Right.Col')")
    left_table, right_table, cond = parts
    lc, rc = cond.split("=", 1)
    join = Join(
        right_table=right_table,
        kind="inner",
        conditions=[JoinCondition(left=_parse_col(lc), right=_parse_col(rc))],
    )
    return left_table, right_table, join


def expand_metric_plan(metric: MetricSpec, *, limit: int | None = None) -> LogicalPlan:
    """Expand a plan or ratio metric to a LogicalPlan.

    For ratio metrics the plan selects the numerator and denominator under
    RATIO_NUMERATOR_ALIAS / RATIO_DENOMINATOR_ALIAS; validate_and_compile_metric
    divides them.
    """
    from uuid import uuid4

    if metric.kind == "builtin" or not metric.source:
        raise ValueError(f"metric {metric.id!r} does not expand to a LogicalPlan")

    if metric.kind == "ratio":
        assert metric.numerator and metric.denominator
        aggregations = [
            _parse_measure(metric.numerator, RATIO_NUMERATOR_ALIAS),
            _parse_measure(metric.denominator, RATIO_DENOMINATOR_ALIAS),
        ]
    else:
        aggregations = [_parse_agg(a) for a in metric.aggregations]

    return LogicalPlan(
        plan_id=uuid4(),
        source=metric.source,
        joins=[_parse_join(j)[2] for j in metric.joins],
        group_by=GroupBy(columns=[_parse_col(c) for c in metric.group_by]) if metric.group_by else None,
        aggregations=aggregations,
        limit=limit or metric.default_limit,
    )


# Code-registered AST builders — no string SQL, no LLM input.
_BUILTIN_BUILDERS: dict[str, Callable[[], exp.Select]] = {}


def register_builtin(id: str, builder: Callable[[], exp.Select]) -> None:
    _BUILTIN_BUILDERS[id] = builder


def compile_builtin_metric(builder_id: str, *, dialect: str = "duckdb") -> str:
    if builder_id not in _BUILTIN_BUILDERS:
        raise ValueError(f"unknown builtin metric: {builder_id!r}")
    return _BUILTIN_BUILDERS[builder_id]().sql(dialect=dialect, pretty=False)


def metric_tables(metric: MetricSpec) -> frozenset[str]:
    """Every table a metric reads. Used to filter metrics onto a principal catalog."""
    tables: set[str] = set(metric.tables)
    if metric.source:
        tables.add(metric.source)
    for spec in metric.joins:
        left_table, right_table, _join = _parse_join(spec)
        tables.add(left_table)
        tables.add(right_table)
    for ref in metric.group_by:
        tables.add(_parse_col(ref).table_id)
    measures = list(metric.aggregations)
    measures += [f"{m}:_" for m in (metric.numerator, metric.denominator) if m]
    for spec in measures:
        agg = _parse_agg(spec)
        if agg.column is not None:
            tables.add(agg.column.table_id)
    return frozenset(tables)


def metrics_for_catalog(catalog: Catalog) -> list[MetricSpec]:
    """Approved metrics defined on this catalog."""
    return list(catalog.metrics)


def get_metric(metric_id: str, catalog: Catalog) -> MetricSpec | None:
    return next((m for m in catalog.metrics if m.id == metric_id), None)


def assert_metric_authorized(metric: MetricSpec, catalog: Catalog) -> None:
    """Refuse metrics whose tables are not all in the (already sliced) catalog."""
    visible = set(catalog.table_map())
    missing = sorted(metric_tables(metric) - visible)
    if missing:
        raise PermissionError(
            f"metric {metric.id!r} requires tables not in this catalog: {missing}"
        )
