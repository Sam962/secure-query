"""Databricks catalog draft + execute (no live warehouse)."""

from __future__ import annotations

from pathlib import Path

from secure_query.auth import Principal
from secure_query.kernel.compile import CompiledQuery
from secure_query.engine.databricks import (
    draft_catalog,
    execute_databricks,
    map_uc_dtype,
    pii_risk_from_tags,
)
from secure_query.engine.execute import ExecuteOptions, ExecutionError
import pytest


def test_map_uc_dtype() -> None:
    assert map_uc_dtype("BIGINT") == "int"
    assert map_uc_dtype("decimal(18,2)") == "float"
    assert map_uc_dtype("STRING") == "str"
    assert map_uc_dtype("timestamp") == "datetime"


def test_pii_tags_mark_high() -> None:
    assert pii_risk_from_tags(["PII"]) == "high"
    assert pii_risk_from_tags(["email"]) == "high"
    assert pii_risk_from_tags(["public"]) == "none"


def test_draft_catalog_from_information_schema_rows() -> None:
    catalog = draft_catalog(
        tenant_id="sales",
        columns=[
            {
                "table_name": "orders",
                "column_name": "amount",
                "data_type": "double",
            },
            {
                "table_name": "orders",
                "column_name": "customer_id",
                "data_type": "bigint",
            },
            {
                "table_name": "customers",
                "column_name": "customer_id",
                "data_type": "bigint",
            },
            {
                "table_name": "customers",
                "column_name": "email",
                "data_type": "string",
                "tags": ["pii"],
            },
        ],
        foreign_keys=[
            {
                "left_table": "orders",
                "left_column": "customer_id",
                "right_table": "customers",
                "right_column": "customer_id",
            }
        ],
        display_columns={"customers": "customers.email"},
    )
    assert catalog.sql_dialect == "databricks"
    assert catalog.tenant_id == "sales"
    assert {t.name for t in catalog.tables} == {"orders", "customers"}
    email = catalog.get_column("customers", "email")
    assert email is not None and email.pii_risk == "high"
    assert catalog.join_allowed("orders", "customer_id", "customers", "customer_id")


class _FakeCursor:
    def __init__(self, rows: list[tuple], description: list[tuple[str, ...]]) -> None:
        self._rows = rows
        self.description = description
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchmany(self, n: int) -> list[tuple]:
        return self._rows[:n]

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, rows: list[tuple], columns: list[str]) -> None:
        self._rows = rows
        self._columns = columns
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, [(c,) for c in self._columns])

    def close(self) -> None:
        self.closed = True


def test_execute_databricks_with_injected_connection(tmp_path: Path) -> None:
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    con = _FakeConnection(rows=[(1,), (2,), (3,)], columns=["n"])
    principal = Principal(principal_id="alice", tenant_id="sales")
    audit_path = tmp_path / "audit.jsonl"
    result = execute_databricks(
        compiled,
        connection=con,
        principal=principal,
        question="how many",
        options=ExecuteOptions(max_rows=2, audit_path=audit_path),
    )
    assert result.columns == ["n"]
    assert len(result.rows) == 2
    assert result.truncated is True
    assert result.audit.backend == "databricks"
    assert result.audit.principal_id == "alice"
    assert result.audit.tenant_id == "sales"
    assert audit_path.exists()


def test_execute_databricks_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])

    def hang(*_a, **_k):
        import time

        time.sleep(2)
        return ["n"], [], False

    monkeypatch.setattr("secure_query.engine.databricks._execute_once", hang)
    with pytest.raises(ExecutionError, match="timeout"):
        execute_databricks(
            compiled,
            connection=_FakeConnection(rows=[], columns=["n"]),
            options=ExecuteOptions(timeout_seconds=0.1),
        )
