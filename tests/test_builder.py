"""DT-216: Fluent LQP builder tests.

One test per intent type that asserts the builder output is deep-equal to
a hand-authored LogicalPlan equivalent.  Also covers immutability, the
filter_raw escape hatch, and import from the top-level package.
"""


import pytest
from pydantic import ValidationError

from secure_query.kernel.builder import LQP, _parse_col
from secure_query.kernel.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    Eq,
    GroupBy,
    In,
    Join,
    JoinCondition,
    LiteralValue,
    LogicalPlan,
    OrderBy,
    TimeBucket,
)


def _airports_enplanements_join() -> Join:
    return Join(
        right_table="enplanements",
        kind="inner",
        conditions=[
            JoinCondition(
                left=ColumnRef(table_id="airports", column_id="locid"),
                right=ColumnRef(table_id="enplanements", column_id="locid"),
            )
        ],
    )


def test_lqp_not_instantiable() -> None:
    with pytest.raises(TypeError):
        LQP()  # type: ignore[call-arg]


def test_aggregate_builder_matches_hand_authored() -> None:
    built = (
        LQP.aggregate(table="airports")
        .join("enplanements", on=[("airports.locid", "enplanements.locid")])
        .group_by_columns(["airports.state"])
        .agg("sum", "enplanements.aat", alias="total_enplanements")
        .build()
    )
    expected = LogicalPlan(
        plan_id=built.plan_id,
        source="airports",
        joins=[_airports_enplanements_join()],
        group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
        aggregations=[
            Aggregation(
                fn="sum",
                column=ColumnRef(table_id="enplanements", column_id="aat"),
                alias="total_enplanements",
            )
        ],
    )
    assert built == expected


def test_rank_builder_matches_hand_authored() -> None:
    built = (
        LQP.rank(table="airports")
        .join("enplanements", on=[("airports.locid", "enplanements.locid")])
        .group_by_columns(["airports.state"])
        .agg("sum", "enplanements.aat", alias="total_enplanements")
        .order_by("total_enplanements", direction="desc")
        .limit(10)
        .build()
    )
    expected = LogicalPlan(
        plan_id=built.plan_id,
        source="airports",
        joins=[_airports_enplanements_join()],
        group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
        aggregations=[
            Aggregation(
                fn="sum",
                column=ColumnRef(table_id="enplanements", column_id="aat"),
                alias="total_enplanements",
            )
        ],
        order_by=[OrderBy(alias="total_enplanements", direction="desc")],
        limit=10,
    )
    assert built == expected


def test_compare_builder_matches_hand_authored() -> None:
    built = (
        LQP.compare(left="airports", right="enplanements")
        .join("enplanements", on=[("airports.locid", "enplanements.locid")])
        .filter("airports.state", "eq", "CA")
        .filter("airports.state", "eq", "TX")
        .group_by_columns(["airports.state"])
        .agg("sum", "enplanements.aat", alias="total_enplanements")
        .build()
    )
    expected = LogicalPlan(
        plan_id=built.plan_id,
        source="airports",
        joins=[_airports_enplanements_join()],
        filters=[
            Eq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="CA")),
            Eq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="TX")),
        ],
        group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
        aggregations=[
            Aggregation(fn="sum", column=ColumnRef(table_id="enplanements", column_id="aat"), alias="total_enplanements")
        ],
    )
    assert built == expected


def test_trend_builder_matches_hand_authored() -> None:
    built = (
        LQP.trend(table="enplanements", date_column="enplanements.ayear", grain="year")
        .agg("sum", "enplanements.aat", alias="yearly_enplanements")
        .order_by("enplanements.ayear", direction="asc")
        .build()
    )
    expected = LogicalPlan(
        plan_id=built.plan_id,
        source="enplanements",
        group_by=GroupBy(
            time_buckets=[TimeBucket(column=ColumnRef(table_id="enplanements", column_id="ayear"), grain="year")]
        ),
        aggregations=[
            Aggregation(fn="sum", column=ColumnRef(table_id="enplanements", column_id="aat"), alias="yearly_enplanements")
        ],
        order_by=[OrderBy(column=ColumnRef(table_id="enplanements", column_id="ayear"), direction="asc")],
    )
    assert built == expected


def test_filter_and_list_builder_matches_hand_authored() -> None:
    built = (
        LQP.filter_and_list(table="airports")
        .filter("airports.state", "eq", "CA")
        .order_by("airports.airport_name", direction="asc")
        .build()
    )
    expected = LogicalPlan(
        plan_id=built.plan_id,
        source="airports",
        filters=[Eq(column=ColumnRef(table_id="airports", column_id="state"), value=LiteralValue(type="string", value="CA"))],
        order_by=[OrderBy(column=ColumnRef(table_id="airports", column_id="airport_name"), direction="asc")],
    )
    assert built == expected


def test_builder_is_immutable() -> None:
    base = LQP.aggregate(table="airports")
    with_filter = base.filter("airports.state", "eq", "CA")
    base_plan = base.build()
    filtered_plan = with_filter.build()
    assert base_plan.filters == []
    assert len(filtered_plan.filters) == 1


def test_filter_raw_accepts_pre_built_filter() -> None:
    f = Between(
        column=ColumnRef(table_id="enplanements", column_id="ayear"),
        low=LiteralValue(type="integer", value=2020),
        high=LiteralValue(type="integer", value=2024),
    )
    plan = LQP.aggregate(table="enplanements").filter_raw(f).build()
    assert len(plan.filters) == 1
    assert isinstance(plan.filters[0], Between)


def test_filter_raw_accepts_in_filter() -> None:
    f = In(
        column=ColumnRef(table_id="airports", column_id="state"),
        values=[LiteralValue(type="string", value="CA"), LiteralValue(type="string", value="TX")],
    )
    plan = LQP.filter_and_list(table="airports").filter_raw(f).build()
    assert len(plan.filters) == 1


def test_parse_col_valid() -> None:
    assert _parse_col("airports.state") == ColumnRef(table_id="airports", column_id="state")


def test_parse_col_invalid_no_dot() -> None:
    with pytest.raises(ValueError, match="table.column"):
        _parse_col("airports")


def test_parse_col_invalid_empty_parts() -> None:
    with pytest.raises(ValueError, match="table.column"):
        _parse_col(".state")


def test_build_rejects_invalid_limit() -> None:
    with pytest.raises(ValidationError):
        LQP.rank(table="airports").limit(0).build()


def test_agg_count_star() -> None:
    plan = LQP.aggregate(table="airports").agg("count", None, alias="num_rows").build()
    assert plan.aggregations[0].column is None
    assert plan.aggregations[0].alias == "num_rows"


def test_group_by_columns_accumulates() -> None:
    plan = (
        LQP.aggregate(table="airports")
        .group_by_columns(["airports.state"])
        .group_by_columns(["airports.city"])
        .build()
    )
    assert plan.group_by is not None
    assert len(plan.group_by.columns) == 2
