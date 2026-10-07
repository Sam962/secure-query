"""Postgres dialect compile parity (no live server required)."""

from __future__ import annotations

import sqlglot

from secure_query.demo.chinook import sample_catalog
from secure_query.demo.lqp import LQP
from secure_query.kernel.compile import compile
from secure_query.kernel.metrics import expand_metric_plan, get_metric
from secure_query.kernel.validate import validate_and_compile, validate_and_compile_metric


def test_revenue_plan_compiles_as_postgres() -> None:
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
        .group_by_columns(["Customer.Country"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(10)
        .build()
    )
    compiled = compile(plan, dialect="postgres")
    parsed = sqlglot.parse(compiled.sql, dialect="postgres")
    assert parsed
    assert "SUM" in compiled.sql.upper()


def test_validate_and_compile_respects_postgres_dialect() -> None:
    catalog = sample_catalog().model_copy(update={"sql_dialect": "postgres"})
    plan = (
        LQP.aggregate(table="Invoice")
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(1)
        .build()
    )
    compiled = validate_and_compile(plan, catalog)
    sqlglot.parse(compiled.sql, dialect="postgres")


def test_ratio_metric_postgres_dialect() -> None:
    catalog = sample_catalog().model_copy(update={"sql_dialect": "postgres"})
    metric = get_metric("avg_revenue_per_customer", catalog)
    assert metric is not None
    sql = validate_and_compile_metric(metric, expand_metric_plan(metric), catalog).sql
    sqlglot.parse(sql, dialect="postgres")
    assert "CustomerId" in sql
    assert "DOUBLE PRECISION" in sql


class _FakeCursor:
    description = [("n",)]

    def fetchmany(self, n):
        return [(1,)]


class _FakePg:
    """Stands in for psycopg: records session statements, can cancel the query."""

    class errors:
        class QueryCanceled(Exception):
            pass

    def __init__(self, cancel: bool = False) -> None:
        self.statements: list[str] = []
        self.cancel = cancel
        self.closed = False

    def connect(self, dsn, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self, sql):
        self.statements.append(sql)
        if self.cancel and not sql.startswith("SET"):
            raise self.errors.QueryCanceled("canceling statement due to statement timeout")
        return _FakeCursor()

    def close(self):
        self.closed = True


def test_postgres_session_is_read_only_and_server_timed(monkeypatch) -> None:
    """H7 + M3: the session refuses writes; the server cancels on timeout; no thread pool."""
    import sys

    from secure_query.engine.execute import ExecuteOptions
    from secure_query.engine.postgres import execute_postgres
    from secure_query.kernel.compile import CompiledQuery

    fake = _FakePg()
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    result = execute_postgres(compiled, "postgresql://x", options=ExecuteOptions(timeout_seconds=2))
    assert fake.statements[:2] == ["SET default_transaction_read_only = on", "SET statement_timeout = 2000"]
    assert result.rows == [(1,)] and fake.closed and fake.kwargs["autocommit"] is True


def test_postgres_statement_timeout_is_audited_as_timeout(monkeypatch, tmp_path) -> None:
    import json
    import sys

    import pytest

    from secure_query.engine.execute import ExecuteOptions, ExecutionError
    from secure_query.engine.postgres import execute_postgres
    from secure_query.kernel.compile import CompiledQuery

    fake = _FakePg(cancel=True)
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    audit = tmp_path / "audit.jsonl"
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    with pytest.raises(ExecutionError, match="timeout") as info:
        execute_postgres(compiled, "postgresql://x", options=ExecuteOptions(audit_path=audit))
    record = json.loads(audit.read_text())
    assert record["status"] == "timeout" and record["audit_id"] == info.value.audit_id
    assert fake.closed
