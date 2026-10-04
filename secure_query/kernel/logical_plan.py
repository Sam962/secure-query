"""Logical Query Plan (LQP) Pydantic v2 contracts.

Producer: LLM planner (structured output only) or secure_query.kernel.builder.LQP.
Consumer: secure_query.kernel.validate then secure_query.kernel.compile.

Typed IR between the LLM and the deterministic SQL compiler. The LLM never
produces raw SQL; it produces a LogicalPlan that is validated and compiled by
code you own.

Supported intent types and how they map to node combinations:
    - aggregate: source + aggregations + group_by (+ optional filters)
    - rank: source + aggregations + group_by + order_by + limit
    - compare: source + joins (two tables) + aggregations + group_by
    - trend: source + group_by with TimeBucket + aggregations + order_by
    - filter_and_list: source + filters + order_by (no aggregations)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ColumnRef(BaseModel):
    """Reference to a column within a table in the schema.

    Example:
        >>> ref = ColumnRef(table_id="airports", column_id="state")
    """

    table_id: str
    column_id: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class LiteralValue(BaseModel):
    """A typed literal value used in filters and predicates.

    Only primitive types are allowed; no arbitrary expressions.

    Example:
        >>> val = LiteralValue(type="string", value="CA")
        >>> num = LiteralValue(type="integer", value=2024)
    """

    type: Literal["string", "integer", "float", "boolean", "date", "datetime"]
    value: str | int | float | bool | date | datetime

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def parse_iso_dates(cls, data: object) -> object:
        """JSON has no date type: accept ISO strings for type=date/datetime.

        Without this, {"type": "date", "value": "2011-01-01"} from the planner can
        never validate, so every date-range question is unanswerable.
        """
        if not isinstance(data, dict) or not isinstance(data.get("value"), str):
            return data
        kind = data.get("type")
        if kind not in ("date", "datetime"):
            return data
        raw = data["value"].strip()
        try:
            value = date.fromisoformat(raw) if kind == "date" else datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"LiteralValue {raw!r} is not an ISO {kind}") from exc
        return {**data, "value": value}

    @model_validator(mode="after")
    def validate_type_matches_value(self) -> LiteralValue:
        """Enforce that `type` matches the concrete Python type of `value`."""
        value = self.value
        if self.type == "string" and not isinstance(value, str):
            raise ValueError("LiteralValue.type='string' requires a string value")
        if self.type == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
            raise ValueError("LiteralValue.type='integer' requires an integer value")
        if self.type == "float" and not isinstance(value, float):
            raise ValueError("LiteralValue.type='float' requires a float value")
        if self.type == "boolean" and not isinstance(value, bool):
            raise ValueError("LiteralValue.type='boolean' requires a boolean value")
        if self.type == "date" and not (
            isinstance(value, date) and not isinstance(value, datetime)
        ):
            raise ValueError("LiteralValue.type='date' requires a date value")
        if self.type == "datetime" and not isinstance(value, datetime):
            raise ValueError("LiteralValue.type='datetime' requires a datetime value")
        return self


class TimeBucket(BaseModel):
    """Time-based grouping for trend and time-series queries.

    Example:
        >>> bucket = TimeBucket(
        ...     column=ColumnRef(table_id="enplanements", column_id="ayear"),
        ...     grain="year",
        ... )
    """

    column: ColumnRef
    grain: Literal["hour", "day", "week", "month", "quarter", "year"]

    model_config = ConfigDict(extra="forbid", frozen=True)


class Aggregation(BaseModel):
    """An aggregate function applied to a column (or count(*) when column is None).

    Example:
        >>> agg = Aggregation(fn="sum", column=ColumnRef(table_id="enplanements", column_id="aat"), alias="total_enplanements")
        >>> count_star = Aggregation(fn="count", column=None, alias="num_rows")
    """

    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: ColumnRef | None = None
    alias: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class GroupBy(BaseModel):
    """Grouping specification: plain columns and/or time buckets.

    Example:
        >>> gb = GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")])
        >>> gb_time = GroupBy(
        ...     columns=[],
        ...     time_buckets=[TimeBucket(column=ColumnRef(table_id="enplanements", column_id="ayear"), grain="year")],
        ... )
    """

    columns: list[ColumnRef] = []
    time_buckets: list[TimeBucket] = []

    model_config = ConfigDict(extra="forbid", frozen=True)


class OrderBy(BaseModel):
    """Ordering specification: column reference or aggregate alias with direction.

    Example:
        >>> ob = OrderBy(column=ColumnRef(table_id="airports", column_id="state"), direction="asc")
        >>> ob_alias = OrderBy(alias="total_enplanements", direction="desc")
    """

    column: ColumnRef | None = None
    alias: str | None = None
    direction: Literal["asc", "desc"] = "asc"

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_exactly_one_target(self) -> OrderBy:
        """Exactly one of `column` or `alias` must be provided."""
        has_column = self.column is not None
        has_alias = self.alias is not None
        if has_column == has_alias:
            raise ValueError("OrderBy requires exactly one of `column` or `alias`")
        return self


class JoinCondition(BaseModel):
    """A single equi-join condition between two columns.

    Example:
        >>> cond = JoinCondition(
        ...     left=ColumnRef(table_id="airports", column_id="locid"),
        ...     right=ColumnRef(table_id="enplanements", column_id="locid"),
        ... )
    """

    left: ColumnRef
    right: ColumnRef

    model_config = ConfigDict(extra="forbid", frozen=True)


class Join(BaseModel):
    """A join between two tables with typed conditions.

    Example:
        >>> join = Join(
        ...     right_table="enplanements",
        ...     kind="inner",
        ...     conditions=[
        ...         JoinCondition(
        ...             left=ColumnRef(table_id="airports", column_id="locid"),
        ...             right=ColumnRef(table_id="enplanements", column_id="locid"),
        ...         )
        ...     ],
        ... )
    """

    right_table: str
    kind: Literal["inner", "left", "right", "cross"] = "inner"
    conditions: list[JoinCondition] = []

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_non_cross_has_conditions(self) -> Join:
        """Non-cross joins must specify at least one join condition."""
        if self.kind != "cross" and len(self.conditions) == 0:
            raise ValueError(f"Join kind='{self.kind}' requires at least one condition")
        return self


# --- Filter (Predicate) discriminated union ---


class _FilterBase(BaseModel):
    """Base class for all filter predicates. Not instantiated directly."""

    op: str
    column: ColumnRef

    model_config = ConfigDict(extra="forbid", frozen=True)


class Eq(_FilterBase):
    """Equality: column = value.

    Example:
        >>> f = Eq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="CA"))
    """

    op: Literal["eq"] = "eq"
    value: ColumnRef | LiteralValue


class NotEq(_FilterBase):
    """Inequality: column != value.

    Example:
        >>> f = NotEq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="CA"))
    """

    op: Literal["ne"] = "ne"
    value: ColumnRef | LiteralValue


class Lt(_FilterBase):
    """Less than: column < value.

    Example:
        >>> f = Lt(column=ColumnRef(table_id="enplanements", column_id="ayear"), value=LiteralValue(type="integer", value=2024))
    """

    op: Literal["lt"] = "lt"
    value: ColumnRef | LiteralValue


class Lte(_FilterBase):
    """Less than or equal: column <= value.

    Example:
        >>> f = Lte(column=ColumnRef(table_id="enplanements", column_id="ayear"), value=LiteralValue(type="integer", value=2024))
    """

    op: Literal["lte"] = "lte"
    value: ColumnRef | LiteralValue


class Gt(_FilterBase):
    """Greater than: column > value.

    Example:
        >>> f = Gt(column=ColumnRef(table_id="enplanements", column_id="aat"), value=LiteralValue(type="integer", value=1000000))
    """

    op: Literal["gt"] = "gt"
    value: ColumnRef | LiteralValue


class Gte(_FilterBase):
    """Greater than or equal: column >= value.

    Example:
        >>> f = Gte(column=ColumnRef(table_id="enplanements", column_id="aat"), value=LiteralValue(type="integer", value=1000000))
    """

    op: Literal["gte"] = "gte"
    value: ColumnRef | LiteralValue


class In(_FilterBase):
    """Membership: column IN (values...).

    Example:
        >>> f = In(
        ...     column=ColumnRef(table_id="airports", column_id="state"),
        ...     values=[LiteralValue(type="string", value="CA"), LiteralValue(type="string", value="TX")],
        ... )
    """

    op: Literal["in"] = "in"
    values: list[LiteralValue] = Field(min_length=1)


class NotIn(_FilterBase):
    """Exclusion: column NOT IN (values...).

    Example:
        >>> f = NotIn(
        ...     column=ColumnRef(table_id="airports", column_id="state"),
        ...     values=[LiteralValue(type="string", value="CA")],
        ... )
    """

    op: Literal["not_in"] = "not_in"
    values: list[LiteralValue] = Field(min_length=1)


class Between(_FilterBase):
    """Range: column BETWEEN low AND high.

    Example:
        >>> f = Between(
        ...     column=ColumnRef(table_id="enplanements", column_id="ayear"),
        ...     low=LiteralValue(type="integer", value=2020),
        ...     high=LiteralValue(type="integer", value=2024),
        ... )
    """

    op: Literal["between"] = "between"
    low: LiteralValue
    high: LiteralValue

    @model_validator(mode="after")
    def validate_low_le_high(self) -> Between:
        """Low bound must not exceed high bound for comparable types."""
        if self.low.type == self.high.type and self.low.value > self.high.value:  # type: ignore[operator]
            raise ValueError("Between.low must be <= Between.high")
        return self


class IsNull(_FilterBase):
    """Null check: column IS NULL.

    Example:
        >>> f = IsNull(column=ColumnRef(table_id="airports", column_id="hub_locid"))
    """

    op: Literal["is_null"] = "is_null"


class NotNull(_FilterBase):
    """Not null check: column IS NOT NULL.

    Example:
        >>> f = NotNull(column=ColumnRef(table_id="airports", column_id="hub_locid"))
    """

    op: Literal["not_null"] = "not_null"


class Like(_FilterBase):
    """Pattern match: column LIKE pattern.

    Example:
        >>> f = Like(column=ColumnRef(table_id="airports", column_id="airport_name"), pattern=LiteralValue(type="string", value="%International%"))
    """

    op: Literal["like"] = "like"
    pattern: LiteralValue


Filter: TypeAlias = Annotated[
    Eq | NotEq | Lt | Lte | Gt | Gte | In | NotIn | Between | IsNull | NotNull | Like,
    Field(discriminator="op"),
]
"""Discriminated union of all filter predicate types.

The discriminator is the `op` field, which determines which variant is used
during deserialization.
"""


class AggregateFilter(BaseModel):
    """HAVING predicate on an aggregate the plan computes, referenced by its alias.

    Example (billing countries with more than 20 invoices):
        >>> f = AggregateFilter(alias="invoice_count", op="gt", value=LiteralValue(type="integer", value=20))

    Filters on aggregates are standard SQL, not arithmetic between aggregates, so
    they stay inside the IR boundary (ADR 002).
    """

    alias: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "between"]
    value: LiteralValue | None = None
    low: LiteralValue | None = None
    high: LiteralValue | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_operands(self) -> AggregateFilter:
        if self.op == "between":
            if self.low is None or self.high is None or self.value is not None:
                raise ValueError("AggregateFilter op='between' requires low and high (not value)")
        elif self.value is None or self.low is not None or self.high is not None:
            raise ValueError(f"AggregateFilter op={self.op!r} requires value (not low/high)")
        return self


# --- Top-level LogicalPlan ---


class LogicalPlan(BaseModel):
    """The top-level Logical Query Plan.

    A LogicalPlan represents a single query over one or more tables. It is the
    typed IR between the LLM planner and the deterministic SQL compiler.

    Example (aggregate intent - total enplanements by state):
        >>> plan = LogicalPlan(
        ...     plan_id=UUID("12345678-1234-1234-1234-123456789abc"),
        ...     schema_version="lqp/1",
        ...     source="airports",
        ...     joins=[
        ...         Join(
        ...             right_table="enplanements",
        ...             kind="inner",
        ...             conditions=[
        ...                 JoinCondition(
        ...                     left=ColumnRef(table_id="airports", column_id="locid"),
        ...                     right=ColumnRef(table_id="enplanements", column_id="locid"),
        ...                 )
        ...             ],
        ...         )
        ...     ],
        ...     filters=[
        ...         Gte(column=ColumnRef(table_id="enplanements", column_id="ayear"), value=LiteralValue(type="integer", value=2020))
        ...     ],
        ...     group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
        ...     aggregations=[Aggregation(fn="sum", column=ColumnRef(table_id="enplanements", column_id="aat"), alias="total_enplanements")],
        ...     order_by=[OrderBy(alias="total_enplanements", direction="desc")],
        ...     limit=10,
        ... )

    Example (filter_and_list intent - airports in California):
        >>> plan = LogicalPlan(
        ...     plan_id=UUID("12345678-1234-1234-1234-123456789abc"),
        ...     schema_version="lqp/1",
        ...     source="airports",
        ...     filters=[
        ...         Eq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="CA"))
        ...     ],
        ...     order_by=[OrderBy(column=ColumnRef(table_id="airports", column_id="airport_name"), direction="asc")],
        ... )
    """

    plan_id: UUID
    schema_version: Literal["lqp/1"] = "lqp/1"
    source: str
    joins: list[Join] = []
    filters: list[Filter] = []
    group_by: GroupBy | None = None
    aggregations: list[Aggregation] = []
    having: list[AggregateFilter] = []
    order_by: list[OrderBy] = []
    limit: int | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_limit_range(self) -> LogicalPlan:
        """Limit must be in a valid, bounded range when provided."""
        if self.limit is not None and not (1 <= self.limit <= 10000):
            raise ValueError("LogicalPlan.limit must be between 1 and 10000")
        return self

    def to_json(self) -> str:
        """Serialize to a deterministic JSON string.

        Output is compact (no extra whitespace) with keys sorted
        alphabetically at every nesting level. Two LogicalPlan instances
        that compare equal will always produce byte-identical JSON.
        """
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> LogicalPlan:
        """Deserialize a JSON string back into a validated LogicalPlan."""
        return cls.model_validate_json(raw)
