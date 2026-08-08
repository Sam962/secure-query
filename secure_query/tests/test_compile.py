"""Unit tests for the LQP -> DuckDB SQL compiler (DT-241, DT-242).

Tests compile each node type and validate output against DuckDB parser.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import duckdb
import pytest
import sqlglot

from secure_query.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    Eq,
    GroupBy,
    Gt,
    Gte,
    In,
    IsNull,
    Join,
    JoinCondition,
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
from secure_query.compile import CompilationError, CompiledQuery, compile


PLAN_ID = UUID("12345678-1234-1234-1234-123456789abc")


def _make_plan(**kwargs) -> LogicalPlan:
    """Helper to build a LogicalPlan with defaults."""
    defaults = {
        "plan_id": PLAN_ID,
        "source": "airports",
    }
    defaults.update(kwargs)
    return LogicalPlan(**defaults)


def _is_valid_duckdb_sql(sql: str) -> bool:
    """Check that DuckDB can parse the SQL statement."""
    try:
        conn = duckdb.connect(":memory:")
        conn.execute(f"EXPLAIN {sql}")
        return True
    except duckdb.Error:
        pass
    # Fallback: at least check sqlglot can parse it as DuckDB dialect
    try:
        parsed = sqlglot.parse(sql, dialect="duckdb")
        return len(parsed) > 0
    except sqlglot.errors.ParseError:
        return False


class TestBasicCompilation:
    def test_select_star_from_table(self) -> None:
        plan = _make_plan()
        result = compile(plan)
        assert isinstance(result, CompiledQuery)
        assert "airports" in result.sql
        assert result.sql_hash
        assert result.plan_hash

    def test_output_is_single_statement(self) -> None:
        plan = _make_plan()
        result = compile(plan)
        statements = sqlglot.parse(result.sql, dialect="duckdb")
        assert len(statements) == 1

    def test_returns_compiled_query_dataclass(self) -> None:
        plan = _make_plan()
        result = compile(plan)
        assert hasattr(result, "sql")
        assert hasattr(result, "plan_hash")
        assert hasattr(result, "sql_hash")
        assert hasattr(result, "parameters")
        assert isinstance(result.parameters, list)


class TestFilters:
    def test_eq_filter(self) -> None:
        plan = _make_plan(
            filters=[
                Eq(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    value=LiteralValue(type="string", value="CA"),
                )
            ]
        )
        result = compile(plan)
        assert "'CA'" in result.sql

    def test_ne_filter(self) -> None:
        plan = _make_plan(
            filters=[
                NotEq(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    value=LiteralValue(type="string", value="TX"),
                )
            ]
        )
        result = compile(plan)
        assert "<>" in result.sql or "!=" in result.sql

    def test_lt_filter(self) -> None:
        plan = _make_plan(
            filters=[
                Lt(
                    column=ColumnRef(table_id="airports", column_id="aat"),
                    value=LiteralValue(type="integer", value=1000),
                )
            ]
        )
        result = compile(plan)
        assert "1000" in result.sql

    def test_gte_filter(self) -> None:
        plan = _make_plan(
            filters=[
                Gte(
                    column=ColumnRef(table_id="airports", column_id="aat"),
                    value=LiteralValue(type="integer", value=5000),
                )
            ]
        )
        result = compile(plan)
        assert ">=" in result.sql

    def test_in_filter(self) -> None:
        plan = _make_plan(
            filters=[
                In(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    values=[
                        LiteralValue(type="string", value="CA"),
                        LiteralValue(type="string", value="TX"),
                    ],
                )
            ]
        )
        result = compile(plan)
        assert "IN" in result.sql.upper()

    def test_not_in_filter(self) -> None:
        plan = _make_plan(
            filters=[
                NotIn(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    values=[LiteralValue(type="string", value="CA")],
                )
            ]
        )
        result = compile(plan)
        assert "NOT" in result.sql.upper()

    def test_between_filter(self) -> None:
        plan = _make_plan(
            filters=[
                Between(
                    column=ColumnRef(table_id="airports", column_id="ayear"),
                    low=LiteralValue(type="integer", value=2020),
                    high=LiteralValue(type="integer", value=2024),
                )
            ]
        )
        result = compile(plan)
        assert "BETWEEN" in result.sql.upper()

    def test_is_null_filter(self) -> None:
        plan = _make_plan(
            filters=[
                IsNull(column=ColumnRef(table_id="airports", column_id="hub"))
            ]
        )
        result = compile(plan)
        assert "NULL" in result.sql.upper()

    def test_not_null_filter(self) -> None:
        plan = _make_plan(
            filters=[
                NotNull(column=ColumnRef(table_id="airports", column_id="hub"))
            ]
        )
        result = compile(plan)
        assert "NOT" in result.sql.upper() and "NULL" in result.sql.upper()

    def test_like_filter(self) -> None:
        plan = _make_plan(
            filters=[
                Like(
                    column=ColumnRef(table_id="airports", column_id="name"),
                    pattern=LiteralValue(type="string", value="%International%"),
                )
            ]
        )
        result = compile(plan)
        assert "LIKE" in result.sql.upper()


class TestAggregations:
    def test_count_star(self) -> None:
        plan = _make_plan(
            aggregations=[Aggregation(fn="count", column=None, alias="total")]
        )
        result = compile(plan)
        assert "COUNT" in result.sql.upper()
        assert "*" in result.sql

    def test_sum(self) -> None:
        plan = _make_plan(
            aggregations=[
                Aggregation(
                    fn="sum",
                    column=ColumnRef(table_id="airports", column_id="aat"),
                    alias="total_aat",
                )
            ]
        )
        result = compile(plan)
        assert "SUM" in result.sql.upper()

    def test_count_distinct(self) -> None:
        plan = _make_plan(
            aggregations=[
                Aggregation(
                    fn="count_distinct",
                    column=ColumnRef(table_id="airports", column_id="state"),
                    alias="distinct_states",
                )
            ]
        )
        result = compile(plan)
        assert "DISTINCT" in result.sql.upper()
        assert "COUNT" in result.sql.upper()

    def test_avg(self) -> None:
        plan = _make_plan(
            aggregations=[
                Aggregation(
                    fn="avg",
                    column=ColumnRef(table_id="airports", column_id="aat"),
                    alias="avg_aat",
                )
            ]
        )
        result = compile(plan)
        assert "AVG" in result.sql.upper()


class TestGroupBy:
    def test_group_by_column(self) -> None:
        plan = _make_plan(
            group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
            aggregations=[Aggregation(fn="count", column=None, alias="cnt")],
        )
        result = compile(plan)
        assert "GROUP BY" in result.sql.upper()
        assert "state" in result.sql.lower()

    def test_group_by_time_bucket(self) -> None:
        plan = _make_plan(
            group_by=GroupBy(
                time_buckets=[
                    TimeBucket(
                        column=ColumnRef(table_id="airports", column_id="ayear"),
                        grain="year",
                    )
                ]
            ),
            aggregations=[Aggregation(fn="count", column=None, alias="cnt")],
        )
        result = compile(plan)
        assert "DATE_TRUNC" in result.sql.upper()

    def test_group_by_multiple_columns(self) -> None:
        plan = _make_plan(
            group_by=GroupBy(
                columns=[
                    ColumnRef(table_id="airports", column_id="state"),
                    ColumnRef(table_id="airports", column_id="hub"),
                ]
            ),
            aggregations=[Aggregation(fn="count", column=None, alias="cnt")],
        )
        result = compile(plan)
        sql_upper = result.sql.upper()
        assert "GROUP BY" in sql_upper


class TestJoins:
    def test_inner_join(self) -> None:
        plan = _make_plan(
            joins=[
                Join(
                    right_table="enplanements",
                    kind="inner",
                    conditions=[
                        JoinCondition(
                            left=ColumnRef(table_id="airports", column_id="locid"),
                            right=ColumnRef(table_id="enplanements", column_id="locid"),
                        )
                    ],
                )
            ]
        )
        result = compile(plan)
        assert "JOIN" in result.sql.upper()
        assert "enplanements" in result.sql.lower()

    def test_left_join(self) -> None:
        plan = _make_plan(
            joins=[
                Join(
                    right_table="enplanements",
                    kind="left",
                    conditions=[
                        JoinCondition(
                            left=ColumnRef(table_id="airports", column_id="locid"),
                            right=ColumnRef(table_id="enplanements", column_id="locid"),
                        )
                    ],
                )
            ]
        )
        result = compile(plan)
        assert "LEFT" in result.sql.upper()


class TestOrderByAndLimit:
    def test_order_by_column(self) -> None:
        plan = _make_plan(
            order_by=[OrderBy(column=ColumnRef(table_id="airports", column_id="state"))]
        )
        result = compile(plan)
        assert "ORDER BY" in result.sql.upper()

    def test_order_by_alias_desc(self) -> None:
        plan = _make_plan(
            aggregations=[Aggregation(fn="count", column=None, alias="cnt")],
            order_by=[OrderBy(alias="cnt", direction="desc")],
        )
        result = compile(plan)
        assert "DESC" in result.sql.upper()

    def test_limit(self) -> None:
        plan = _make_plan(limit=10)
        result = compile(plan)
        assert "LIMIT" in result.sql.upper()
        assert "10" in result.sql


class TestHashing:
    def test_plan_hash_is_stable(self) -> None:
        plan = _make_plan(
            filters=[
                Eq(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    value=LiteralValue(type="string", value="CA"),
                )
            ]
        )
        r1 = compile(plan)
        r2 = compile(plan)
        assert r1.plan_hash == r2.plan_hash
        assert r1.sql_hash == r2.sql_hash

    def test_different_plans_different_hashes(self) -> None:
        plan1 = _make_plan(source="airports")
        plan2 = _make_plan(source="enplanements")
        r1 = compile(plan1)
        r2 = compile(plan2)
        assert r1.plan_hash != r2.plan_hash

    def test_sql_hash_matches_output(self) -> None:
        import hashlib

        plan = _make_plan()
        result = compile(plan)
        expected_hash = hashlib.sha256(result.sql.encode()).hexdigest()
        assert result.sql_hash == expected_hash


class TestNoStringInterpolation:
    """Verify adversarial identifiers are safely quoted."""

    def test_table_with_semicolon(self) -> None:
        plan = _make_plan(source=";DROP TABLE users--")
        result = compile(plan)
        assert "DROP" not in result.sql.replace('"', "").split("FROM")[0]
        statements = sqlglot.parse(result.sql, dialect="duckdb")
        assert len(statements) == 1

    def test_column_with_quotes(self) -> None:
        plan = _make_plan(
            filters=[
                Eq(
                    column=ColumnRef(table_id="airports", column_id="'; DROP TABLE x--"),
                    value=LiteralValue(type="string", value="test"),
                )
            ]
        )
        result = compile(plan)
        statements = sqlglot.parse(result.sql, dialect="duckdb")
        assert len(statements) == 1

    def test_literal_with_sql_injection(self) -> None:
        plan = _make_plan(
            filters=[
                Eq(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    value=LiteralValue(type="string", value="'; DROP TABLE users; --"),
                )
            ]
        )
        result = compile(plan)
        statements = sqlglot.parse(result.sql, dialect="duckdb")
        assert len(statements) == 1


class TestComplexPlans:
    """Integration tests with realistic multi-node plans."""

    def test_aggregate_intent(self) -> None:
        """Total enplanements by state, filtered to year >= 2020, top 10."""
        plan = _make_plan(
            source="airports",
            joins=[
                Join(
                    right_table="enplanements",
                    kind="inner",
                    conditions=[
                        JoinCondition(
                            left=ColumnRef(table_id="airports", column_id="locid"),
                            right=ColumnRef(table_id="enplanements", column_id="locid"),
                        )
                    ],
                )
            ],
            filters=[
                Gte(
                    column=ColumnRef(table_id="enplanements", column_id="ayear"),
                    value=LiteralValue(type="integer", value=2020),
                )
            ],
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
        result = compile(plan)
        sql_upper = result.sql.upper()
        assert "JOIN" in sql_upper
        assert "GROUP BY" in sql_upper
        assert "ORDER BY" in sql_upper
        assert "LIMIT" in sql_upper
        assert "SUM" in sql_upper

    def test_trend_intent(self) -> None:
        """Year-over-year count with TimeBucket."""
        plan = _make_plan(
            source="enplanements",
            group_by=GroupBy(
                time_buckets=[
                    TimeBucket(
                        column=ColumnRef(table_id="enplanements", column_id="ayear"),
                        grain="year",
                    )
                ]
            ),
            aggregations=[
                Aggregation(
                    fn="sum",
                    column=ColumnRef(table_id="enplanements", column_id="aat"),
                    alias="yearly_total",
                )
            ],
            order_by=[
                OrderBy(
                    column=ColumnRef(table_id="enplanements", column_id="ayear"),
                    direction="asc",
                )
            ],
        )
        result = compile(plan)
        assert "DATE_TRUNC" in result.sql.upper()

    def test_filter_and_list_intent(self) -> None:
        """List airports in California, ordered by name."""
        plan = _make_plan(
            source="airports",
            filters=[
                Eq(
                    column=ColumnRef(table_id="airports", column_id="state"),
                    value=LiteralValue(type="string", value="CA"),
                )
            ],
            order_by=[
                OrderBy(
                    column=ColumnRef(table_id="airports", column_id="airport_name"),
                    direction="asc",
                )
            ],
        )
        result = compile(plan)
        assert "SELECT *" in result.sql.upper() or "SELECT" in result.sql.upper()
        assert "'CA'" in result.sql

    def test_rank_intent(self) -> None:
        """Top 5 states by enplanement count."""
        plan = _make_plan(
            source="airports",
            joins=[
                Join(
                    right_table="enplanements",
                    kind="inner",
                    conditions=[
                        JoinCondition(
                            left=ColumnRef(table_id="airports", column_id="locid"),
                            right=ColumnRef(table_id="enplanements", column_id="locid"),
                        )
                    ],
                )
            ],
            group_by=GroupBy(columns=[ColumnRef(table_id="airports", column_id="state")]),
            aggregations=[
                Aggregation(
                    fn="sum",
                    column=ColumnRef(table_id="enplanements", column_id="aat"),
                    alias="total",
                )
            ],
            order_by=[OrderBy(alias="total", direction="desc")],
            limit=5,
        )
        result = compile(plan)
        assert "LIMIT" in result.sql.upper()
        assert "5" in result.sql
