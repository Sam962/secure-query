from datetime import date
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from secure_query.kernel.logical_plan import (
    Between,
    ColumnRef,
    Eq,
    Filter,
    In,
    Join,
    LiteralValue,
    LogicalPlan,
    NotIn,
    OrderBy,
)

# --- LiteralValue type enforcement ---


def test_literal_value_type_mismatch_rejected() -> None:
    # Structured outputs send literals as strings: a numeric string is coerced by
    # `type`, anything else is still rejected.
    assert LiteralValue(type="integer", value="2024").value == 2024
    for kind, bad in (("integer", "abc"), ("float", "x1"), ("boolean", "yes"), ("integer", "1.5")):
        with pytest.raises(ValidationError):
            LiteralValue(type=kind, value=bad)


def test_literal_value_correct_type_accepted() -> None:
    value = LiteralValue(type="integer", value=2024)
    assert value.value == 2024


def test_literal_value_boolean_not_accepted_as_integer() -> None:
    with pytest.raises(ValidationError):
        LiteralValue(type="integer", value=True)


def test_literal_value_date_accepted() -> None:
    val = LiteralValue(type="date", value=date(2024, 1, 1))
    assert val.value == date(2024, 1, 1)


# --- extra="forbid" enforcement ---


def test_extra_fields_rejected_on_column_ref() -> None:
    with pytest.raises(ValidationError):
        ColumnRef(table_id="airports", column_id="state", bogus="oops")  # type: ignore[call-arg]


def test_extra_fields_rejected_on_logical_plan() -> None:
    with pytest.raises(ValidationError):
        LogicalPlan(plan_id=uuid4(), source="airports", secret="nope")  # type: ignore[call-arg]


# --- frozen=True enforcement ---


def test_frozen_rejects_mutation_on_column_ref() -> None:
    ref = ColumnRef(table_id="airports", column_id="state")
    with pytest.raises(ValidationError):
        ref.table_id = "other"


def test_frozen_rejects_mutation_on_logical_plan() -> None:
    plan = LogicalPlan(plan_id=uuid4(), source="airports")
    with pytest.raises(ValidationError):
        plan.source = "other"


# --- OrderBy XOR constraint ---


def test_order_by_requires_exactly_one_of_column_or_alias_both_set() -> None:
    with pytest.raises(ValidationError):
        OrderBy(
            column=ColumnRef(table_id="airports", column_id="state"),
            alias="state_alias",
            direction="asc",
        )


def test_order_by_requires_exactly_one_of_column_or_alias_neither_set() -> None:
    with pytest.raises(ValidationError):
        OrderBy(direction="asc")


def test_order_by_with_alias_only_is_valid() -> None:
    order = OrderBy(alias="total_enplanements", direction="desc")
    assert order.alias == "total_enplanements"
    assert order.column is None


# --- LogicalPlan limit range ---


def test_logical_plan_limit_below_range_rejected() -> None:
    with pytest.raises(ValidationError):
        LogicalPlan(plan_id=uuid4(), source="airports", limit=0)


def test_logical_plan_limit_above_range_rejected() -> None:
    with pytest.raises(ValidationError):
        LogicalPlan(plan_id=uuid4(), source="airports", limit=10001)


def test_logical_plan_limit_within_range_accepted() -> None:
    plan = LogicalPlan(plan_id=uuid4(), source="airports", limit=100)
    assert plan.limit == 100


def test_logical_plan_no_limit_accepted() -> None:
    plan = LogicalPlan(plan_id=uuid4(), source="airports")
    assert plan.limit is None


# --- Filter discriminated union ---


def test_filter_discriminated_union_deserializes_correct_variant() -> None:
    adapter: TypeAdapter[Filter] = TypeAdapter(Filter)
    raw = {
        "op": "eq",
        "column": {"table_id": "airports", "column_id": "state"},
        "value": {"type": "string", "value": "CA"},
    }
    result = adapter.validate_python(raw)
    assert isinstance(result, Eq)
    assert result.column.column_id == "state"


def test_filter_discriminated_union_rejects_unknown_op() -> None:
    adapter: TypeAdapter[Filter] = TypeAdapter(Filter)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "op": "regex",
                "column": {"table_id": "airports", "column_id": "state"},
                "pattern": "foo",
            }
        )


# --- Between low <= high ---


def test_between_low_greater_than_high_rejected() -> None:
    col = ColumnRef(table_id="enplanements", column_id="ayear")
    with pytest.raises(ValidationError):
        Between(
            column=col,
            low=LiteralValue(type="integer", value=2024),
            high=LiteralValue(type="integer", value=2020),
        )


def test_between_equal_bounds_accepted() -> None:
    col = ColumnRef(table_id="enplanements", column_id="ayear")
    f = Between(
        column=col,
        low=LiteralValue(type="integer", value=2024),
        high=LiteralValue(type="integer", value=2024),
    )
    assert f.low.value == f.high.value


# --- In / NotIn empty values ---


def test_in_empty_values_rejected() -> None:
    col = ColumnRef(table_id="airports", column_id="state")
    with pytest.raises(ValidationError):
        In(column=col, values=[])


def test_not_in_empty_values_rejected() -> None:
    col = ColumnRef(table_id="airports", column_id="state")
    with pytest.raises(ValidationError):
        NotIn(column=col, values=[])


def test_in_with_values_accepted() -> None:
    col = ColumnRef(table_id="airports", column_id="state")
    f = In(column=col, values=[LiteralValue(type="string", value="CA")])
    assert len(f.values) == 1


# --- Join conditions guard ---


def test_join_inner_without_conditions_rejected() -> None:
    with pytest.raises(ValidationError):
        Join(right_table="enplanements", kind="inner", conditions=[])


def test_join_cross_without_conditions_accepted() -> None:
    j = Join(right_table="enplanements", kind="cross", conditions=[])
    assert j.kind == "cross"
