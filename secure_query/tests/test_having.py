"""HAVING filters target an aggregate the plan computes, not a table column."""

from __future__ import annotations

import json

import duckdb
import pytest

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.kernel.logical_plan import AggregateFilter, LiteralValue, LogicalPlan
from secure_query.kernel.validate import PlanValidationFailed, validate, validate_and_compile
from secure_query.planner import parse_plan_json

_DB = "data/chinook.duckdb"


def _plan(having: list[dict], **extra) -> dict:
    return {
        "source": "Invoice",
        "group_by": {"columns": [{"table_id": "Invoice", "column_id": "BillingCountry"}], "time_buckets": []},
        "aggregations": [{"fn": "count", "column": None, "alias": "invoice_count"}],
        "having": having,
        "limit": 100,
        **extra,
    }


def test_having_on_aggregate_alias_compiles_and_runs() -> None:
    catalog = sample_catalog()
    raw = json.dumps(
        _plan([{"alias": "invoice_count", "op": "gt", "value": {"type": "integer", "value": 20}}])
    )
    compiled = validate_and_compile(parse_plan_json(raw, catalog), catalog)
    assert "HAVING COUNT(*) > 20" in compiled.sql
    con = duckdb.connect(_DB, read_only=True)
    got = sorted(con.execute(compiled.sql).fetchall())
    want = sorted(
        con.execute(
            "SELECT BillingCountry, COUNT(*) FROM Invoice GROUP BY BillingCountry HAVING COUNT(*) > 20"
        ).fetchall()
    )
    assert got == want


def test_column_shaped_having_naming_an_alias_is_read_as_aggregate_filter() -> None:
    """Models often write the filter shape; the alias makes the meaning unambiguous."""
    catalog = sample_catalog()
    raw = json.dumps(
        _plan(
            [
                {
                    "op": "gt",
                    "column": {"table_id": "Invoice", "column_id": "invoice_count"},
                    "value": {"type": "integer", "value": 20},
                }
            ]
        )
    )
    plan = parse_plan_json(raw, catalog)
    assert plan.having == [
        AggregateFilter(alias="invoice_count", op="gt", value=LiteralValue(type="integer", value=20))
    ]


def test_having_unknown_alias_is_rejected() -> None:
    plan = LogicalPlan.model_validate(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            **_plan([{"alias": "nope", "op": "gt", "value": {"type": "integer", "value": 1}}]),
        }
    )
    codes = {e.code for e in validate(plan, sample_catalog())}
    assert "plan.unknown_having_alias" in codes


def test_having_value_type_must_be_numeric_for_numeric_aggregate() -> None:
    plan = LogicalPlan.model_validate(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            **_plan([{"alias": "invoice_count", "op": "gt", "value": {"type": "string", "value": "x"}}]),
        }
    )
    with pytest.raises(PlanValidationFailed, match="having"):
        validate_and_compile(plan, sample_catalog())


def test_having_between_and_sum_alias_on_postgres() -> None:
    catalog = sample_catalog().model_copy(update={"sql_dialect": "postgres"})
    raw = json.dumps(
        {
            "source": "Invoice",
            "group_by": {"columns": [{"table_id": "Invoice", "column_id": "BillingCountry"}], "time_buckets": []},
            "aggregations": [{"fn": "sum", "column": {"table_id": "Invoice", "column_id": "Total"}, "alias": "revenue"}],
            "having": [
                {
                    "alias": "revenue",
                    "op": "between",
                    "low": {"type": "float", "value": 50.0},
                    "high": {"type": "float", "value": 200.0},
                }
            ],
            "limit": 100,
        }
    )
    sql = validate_and_compile(parse_plan_json(raw, catalog), catalog).sql
    assert 'HAVING SUM("Invoice"."Total") BETWEEN 50.0 AND 200.0' in sql
