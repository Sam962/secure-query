"""Fluent builder API for constructing LogicalPlan instances.

Useful for tests and fixtures. Production planners should emit LogicalPlan JSON
directly (structured LLM output), then validate + compile.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from secure_query.kernel.logical_plan import (
    Aggregation,
    ColumnRef,
    Eq,
    Filter,
    GroupBy,
    Join,
    JoinCondition,
    LiteralValue,
    LogicalPlan,
    OrderBy,
    TimeBucket,
)

_AggFn = Literal["count", "count_distinct", "sum", "avg", "min", "max"]
_Direction = Literal["asc", "desc"]
_Grain = Literal["hour", "day", "week", "month", "quarter", "year"]


def _parse_col(ref: str) -> ColumnRef:
    """Parse a 'table.column' string into a ColumnRef."""
    parts = ref.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Column reference must be 'table.column', got: {ref!r}")
    return ColumnRef(table_id=parts[0], column_id=parts[1])


def _make_join(right_table: str, on: list[tuple[str, str]]) -> Join:
    """Build a Join from a list of (left_col, right_col) string pairs."""
    conditions = [JoinCondition(left=_parse_col(l), right=_parse_col(r)) for l, r in on]
    return Join(right_table=right_table, kind="inner", conditions=conditions)


def _make_filter(col: str, op: str, value: str | int | float | bool) -> Filter:
    """Build an Eq filter. Only equality is supported in this initial API."""
    if op != "eq":
        raise ValueError(
            f"Builder .filter() only supports op='eq' for now; got {op!r}. "
            "Construct Filter objects directly for other predicates."
        )
    column = _parse_col(col)
    if isinstance(value, bool):
        lit = LiteralValue(type="boolean", value=value)
    elif isinstance(value, int):
        lit = LiteralValue(type="integer", value=value)
    elif isinstance(value, float):
        lit = LiteralValue(type="float", value=value)
    else:
        lit = LiteralValue(type="string", value=str(value))
    return Eq(column=column, value=lit)


class _PlanBuilder:
    """Internal base builder. Immutable: every mutation returns a new copy."""

    def __init__(
        self,
        source: str,
        *,
        plan_id: UUID | None = None,
        joins: list[Join] | None = None,
        filters: list[Filter] | None = None,
        group_by: GroupBy | None = None,
        aggregations: list[Aggregation] | None = None,
        having: list[Filter] | None = None,
        order_by: list[OrderBy] | None = None,
        limit: int | None = None,
    ) -> None:
        self._source = source
        self._plan_id: UUID = plan_id or uuid4()
        self._joins: list[Join] = joins or []
        self._filters: list[Filter] = filters or []
        self._group_by: GroupBy | None = group_by
        self._aggregations: list[Aggregation] = aggregations or []
        self._having: list[Filter] = having or []
        self._order_by: list[OrderBy] = order_by or []
        self._limit: int | None = limit

    def _copy(self, **overrides: object) -> _PlanBuilder:
        """Return a shallow copy of this builder with the given overrides."""
        return self.__class__(
            overrides.get("source", self._source),  # type: ignore[arg-type]
            plan_id=overrides.get("plan_id", self._plan_id),  # type: ignore[arg-type]
            joins=overrides.get("joins", list(self._joins)),  # type: ignore[arg-type]
            filters=overrides.get("filters", list(self._filters)),  # type: ignore[arg-type]
            group_by=overrides.get("group_by", self._group_by),  # type: ignore[arg-type]
            aggregations=overrides.get("aggregations", list(self._aggregations)),  # type: ignore[arg-type]
            having=overrides.get("having", list(self._having)),  # type: ignore[arg-type]
            order_by=overrides.get("order_by", list(self._order_by)),  # type: ignore[arg-type]
            limit=overrides.get("limit", self._limit),  # type: ignore[arg-type]
        )

    def join(self, right_table: str, on: list[tuple[str, str]]) -> _PlanBuilder:
        """Add an inner join."""
        return self._copy(joins=list(self._joins) + [_make_join(right_table, on)])

    def filter(self, col: str, op: str, value: str | int | float | bool) -> _PlanBuilder:
        """Add a WHERE predicate (currently only op='eq')."""
        return self._copy(filters=list(self._filters) + [_make_filter(col, op, value)])

    def filter_raw(self, f: Filter) -> _PlanBuilder:
        """Add a pre-constructed Filter directly."""
        return self._copy(filters=list(self._filters) + [f])

    def group_by_columns(self, cols: list[str]) -> _PlanBuilder:
        """Set or extend GROUP BY with plain column references."""
        existing = self._group_by
        new_cols = [_parse_col(c) for c in cols]
        if existing is None:
            gb = GroupBy(columns=new_cols)
        else:
            gb = GroupBy(
                columns=list(existing.columns) + new_cols,
                time_buckets=list(existing.time_buckets),
            )
        return self._copy(group_by=gb)

    def group_by_time(self, col: str, grain: _Grain) -> _PlanBuilder:
        """Add a TimeBucket to GROUP BY."""
        bucket = TimeBucket(column=_parse_col(col), grain=grain)
        existing = self._group_by
        if existing is None:
            gb = GroupBy(time_buckets=[bucket])
        else:
            gb = GroupBy(
                columns=list(existing.columns),
                time_buckets=list(existing.time_buckets) + [bucket],
            )
        return self._copy(group_by=gb)

    def agg(self, fn: _AggFn, col: str | None, alias: str) -> _PlanBuilder:
        """Add an aggregation."""
        column = _parse_col(col) if col is not None else None
        a = Aggregation(fn=fn, column=column, alias=alias)
        return self._copy(aggregations=list(self._aggregations) + [a])

    def order_by(self, ref: str, direction: _Direction = "asc") -> _PlanBuilder:
        """Add an ORDER BY clause."""
        if "." in ref:
            ob = OrderBy(column=_parse_col(ref), direction=direction)
        else:
            ob = OrderBy(alias=ref, direction=direction)
        return self._copy(order_by=list(self._order_by) + [ob])

    def limit(self, n: int) -> _PlanBuilder:
        """Set a LIMIT."""
        return self._copy(limit=n)

    def build(self) -> LogicalPlan:
        """Validate and return the final LogicalPlan."""
        return LogicalPlan(
            plan_id=self._plan_id,
            source=self._source,
            joins=self._joins,
            filters=self._filters,
            group_by=self._group_by,
            aggregations=self._aggregations,
            having=self._having,
            order_by=self._order_by,
            limit=self._limit,
        )


class LQP:
    """Entry point for the fluent LogicalPlan builder API."""

    def __init__(self) -> None:  # pragma: no cover
        raise TypeError("LQP is a namespace; do not instantiate it directly.")

    @classmethod
    def aggregate(cls, table: str) -> _PlanBuilder:
        return _PlanBuilder(table)

    @classmethod
    def rank(cls, table: str) -> _PlanBuilder:
        return _PlanBuilder(table)

    @classmethod
    def compare(cls, left: str, right: str) -> _PlanBuilder:
        _ = right
        return _PlanBuilder(left)

    @classmethod
    def trend(cls, table: str, date_column: str, grain: _Grain) -> _PlanBuilder:
        return _PlanBuilder(table).group_by_time(date_column, grain)

    @classmethod
    def filter_and_list(cls, table: str) -> _PlanBuilder:
        return _PlanBuilder(table)
