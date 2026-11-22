"""Databricks catalog draft + execute (no live warehouse)."""

from __future__ import annotations

from pathlib import Path

import pytest

from secure_query.auth import Principal
from secure_query.engine.databricks import (
    draft_catalog,
    execute_databricks,
    map_uc_dtype,
    pii_risk_from_tags,
)
from secure_query.engine.execute import ExecuteOptions, ExecutionError
from secure_query.kernel.compile import CompiledQuery


def test_map_uc_dtype() -> None:
    assert map_uc_dtype("BIGINT") == "int"
    assert map_uc_dtype("decimal(18,2)") == "float"
    assert map_uc_dtype("STRING") == "str"
    assert map_uc_dtype("timestamp") == "datetime"


def test_pii_tags_mark_high() -> None:
    assert pii_risk_from_tags(["PII"]) == "high"
    assert pii_risk_from_tags(["email"]) == "high"
    assert pii_risk_from_tags(["public"]) == "none"
    # Fail closed on sensitive names when UC tags are missing.
    assert pii_risk_from_tags([], column_name="Email") == "high"
    assert pii_risk_from_tags([], column_name="billing_address") == "high"
    assert pii_risk_from_tags(["public"], column_name="Email") == "none"
    assert pii_risk_from_tags([], column_name="Country") == "none"


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
                # no tags — name heuristic must still mark high
            },
            {
                "table_name": "customers",
                "column_name": "phone",
                "data_type": "string",
                "tags": ["public"],  # explicit clear wins over name
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
    phone = catalog.get_column("customers", "phone")
    assert phone is not None and phone.pii_risk == "none"
    assert catalog.join_allowed("orders", "customer_id", "customers", "customer_id")


def test_fetch_foreign_keys_uses_referential_constraints() -> None:
    from secure_query.engine.databricks import _fetch_foreign_keys

    class Cur:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [("orders", "customer_id", "customers", "customer_id")]

    fks = _fetch_foreign_keys(Cur(), catalog_name="main", schema_name="sales")
    assert fks == [
        {
            "left_table": "orders",
            "left_column": "customer_id",
            "right_table": "customers",
            "right_column": "customer_id",
        }
    ]


def test_fetch_foreign_keys_fail_closed_on_error() -> None:
    from secure_query.engine.databricks import _fetch_foreign_keys
    from secure_query.engine.execute import ExecutionError

    class Bad:
        def execute(self, *_a, **_k):
            raise RuntimeError("no such table")

        def fetchall(self):
            return []

    with pytest.raises(ExecutionError, match="foreign-key draft failed"):
        _fetch_foreign_keys(Bad(), catalog_name="main", schema_name="sales")


def test_resolve_warehouse_token_user_vs_shared(monkeypatch) -> None:
    from secure_query.engine.databricks import resolve_warehouse_token
    from secure_query.engine.execute import ExecutionError

    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "user")
    with pytest.raises(ExecutionError, match="end-user Databricks identity"):
        resolve_warehouse_token(Principal(principal_id="a", tenant_id="t"))
    tok = resolve_warehouse_token(
        Principal(principal_id="a", tenant_id="t", warehouse_token="user-pat")
    )
    assert tok == "user-pat"
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "shared")
    monkeypatch.setenv("DATABRICKS_TOKEN", "svc-pat")
    assert resolve_warehouse_token(None) == "svc-pat"


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


def test_databricks_timeout_cancels_the_running_statement() -> None:
    """M3: on timeout the statement is cancelled and the worker joined."""
    import threading

    cancelled = threading.Event()

    class SlowCursor:
        description = [("n",)]

        def execute(self, sql):
            cancelled.wait(5)  # runs until cancelled

        def fetchmany(self, n):
            return []

        def cancel(self):
            cancelled.set()

        def close(self):
            pass

    con = _FakeConnection(rows=[], columns=["n"])
    con.cursor = lambda: SlowCursor()  # type: ignore[method-assign]
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    before = set(threading.enumerate())
    with pytest.raises(ExecutionError, match="timeout"):
        execute_databricks(compiled, connection=con, options=ExecuteOptions(timeout_seconds=0.2))
    assert cancelled.is_set()
    assert not [t for t in set(threading.enumerate()) - before if t.name.startswith("sq-databricks")]


class _GrantCursor:
    def __init__(self, grants: list[tuple[str, str]]) -> None:
        self._grants = grants
        self.description = None

    def execute(self, sql):
        if sql.startswith("SHOW GRANTS"):
            self.description = [("Principal",), ("ActionType",), ("ObjectType",), ("ObjectKey",)]
        self.sql = sql

    def fetchone(self):
        return ("svc-reader@corp",)

    def fetchall(self):
        return [(p, a, "SCHEMA", "main.sales") for p, a in self._grants]

    def close(self):
        pass


def _grant_connection(grants):
    con = _FakeConnection(rows=[], columns=[])
    con.cursor = lambda: _GrantCursor(grants)  # type: ignore[method-assign]
    return con


def test_grant_check_reports_select_only_write_and_unchecked(monkeypatch) -> None:
    from secure_query.engine.databricks import databricks_grant_check

    monkeypatch.delenv("SECURE_QUERY_DATABRICKS_SCHEMA", raising=False)
    assert databricks_grant_check(_grant_connection([]))["status"] == "unchecked"
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_SCHEMA", "main.sales")
    ok = databricks_grant_check(
        _grant_connection([("svc-reader@corp", "SELECT"), ("svc-reader@corp", "USE SCHEMA"), ("admins", "MODIFY")])
    )
    assert ok["status"] == "select_only"  # another principal's MODIFY is not ours
    bad = databricks_grant_check(_grant_connection([("svc-reader@corp", "MODIFY")]))
    assert bad["status"] == "write_privileges" and "MODIFY" in bad["detail"]


def test_connection_pins_the_catalog_and_schema(monkeypatch) -> None:
    """Unqualified table names must resolve in the approved schema, not the session default."""
    import pytest

    from secure_query.engine import databricks
    from secure_query.engine.execute import ExecutionError
    from secure_query.kernel.compile import CompiledQuery

    for key, value in {
        "DATABRICKS_HOST": "https://dbc.example.com",
        "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
        "DATABRICKS_TOKEN": "pat",
        "SECURE_QUERY_DATABRICKS_AUTH": "shared",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SECURE_QUERY_DATABRICKS_SCHEMA", raising=False)
    with pytest.raises(ExecutionError, match="SECURE_QUERY_DATABRICKS_SCHEMA"):
        databricks.databricks_settings_from_env()
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_SCHEMA", "main.sales")
    seen: dict = {}
    monkeypatch.setattr(databricks, "_connect", lambda **kw: seen.update(kw) or _FakeConnection(rows=[], columns=[]))
    databricks.execute_databricks(CompiledQuery(sql="SELECT 1", plan_hash="h", sql_hash="h", parameters=[]))
    assert seen["catalog"] == "main" and seen["schema"] == "sales"
    assert seen["access_token"] == "pat"


def test_execute_uses_end_user_token(monkeypatch) -> None:
    from secure_query.engine import databricks
    from secure_query.kernel.compile import CompiledQuery

    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "svc-must-not-use")
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_SCHEMA", "main.sales")
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "user")
    seen: dict = {}
    monkeypatch.setattr(
        databricks, "_connect", lambda **kw: seen.update(kw) or _FakeConnection(rows=[(1,)], columns=["n"])
    )
    principal = Principal(principal_id="alice", tenant_id="sales", warehouse_token="user-obo")
    databricks.execute_databricks(
        CompiledQuery(sql="SELECT 1", plan_hash="h", sql_hash="h", parameters=[]),
        principal=principal,
    )
    assert seen["access_token"] == "user-obo"


def test_connections_are_reused_per_credential_and_dropped_after_a_failure(monkeypatch) -> None:
    from secure_query.engine import databricks

    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "user")
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_SCHEMA", "main.sales")
    opened: list[_FakeConnection] = []

    def connect(**settings):
        opened.append(_FakeConnection(rows=[(1,)], columns=["n"]))
        opened[-1].token = settings["access_token"]
        return opened[-1]

    monkeypatch.setattr(databricks, "_connect", connect)
    databricks._POOL.close_all()
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    alice = Principal(principal_id="alice", tenant_id="t", warehouse_token="alice-token")
    bob = Principal(principal_id="bob", tenant_id="t", warehouse_token="bob-token")

    for _ in range(3):
        execute_databricks(compiled, principal=alice)
    assert len(opened) == 1 and not opened[0].closed  # three statements, one session
    execute_databricks(compiled, principal=bob)
    assert len(opened) == 2 and opened[1].token == "bob-token"  # never another person's session

    def broken(*_a, **_k):
        raise RuntimeError("statement failed")

    monkeypatch.setattr(databricks, "_execute_once", broken)
    with pytest.raises(Exception):
        execute_databricks(compiled, principal=alice)
    assert opened[0].closed  # a session that failed is not reused
    databricks._POOL.close_all()
    assert opened[1].closed


def test_unity_catalog_comments_become_draft_descriptions() -> None:
    from secure_query.engine.databricks import fetch_unity_catalog_draft

    class Cur:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "information_schema.columns" in self.sql:
                return [("orders", "id", "INT", None), ("orders", "status", "STRING", "A = active")]
            if "information_schema.tables" in self.sql:
                return [("orders", "One row per order")]
            return []

    class Con:
        def cursor(self):
            return Cur()

    catalog = fetch_unity_catalog_draft(Con(), tenant_id="t", catalog_name="main", schema_name="sales")
    (orders,) = catalog.tables
    assert orders.description == "One row per order"
    assert {c.name: c.description for c in orders.columns} == {"id": "", "status": "A = active"}
