"""Runtime catalog load and execute backend dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from secure_query.auth import Principal
from secure_query.engine.execute import ExecuteOptions
from secure_query.engine.runtime import (
    databricks_configured,
    execute_compiled_query,
    load_active_catalog,
    runtime_config,
)
from secure_query.kernel.compile import CompiledQuery


def test_databricks_configured_false_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DATABRICKS_HOST",
        "DATABRICKS_SERVER_HOSTNAME",
        "DATABRICKS_HTTP_PATH",
        "DATABRICKS_TOKEN",
        "DATABRICKS_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    assert databricks_configured() is False
    assert runtime_config().backend == "duckdb"


def test_databricks_configured_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    assert databricks_configured() is True
    assert runtime_config().backend == "databricks"


def test_load_active_catalog_defaults_to_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURE_QUERY_CATALOG_FILE", raising=False)
    catalog = load_active_catalog()
    assert catalog.tenant_id == "chinook"
    assert any(t.name == "Invoice" for t in catalog.tables)


def test_execute_compiled_query_uses_databricks_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")

    called: dict[str, object] = {}

    def fake_execute_databricks(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        from secure_query.engine.execute import ExecutionResult, _make_audit

        compiled = args[0] if args else kwargs["compiled"]
        principal = kwargs.get("principal")
        audit = _make_audit(
            status="ok",
            compiled=compiled,
            plan=None,
            question="q",
            principal=principal,
            row_count=1,
            truncated=False,
            duration_ms=1.0,
            backend="databricks",
        )
        return ExecutionResult(
            columns=["n"],
            rows=[(1,)],
            truncated=False,
            duration_ms=1.0,
            audit=audit,
            compiled=compiled,
        )

    monkeypatch.setattr("secure_query.engine.runtime.execute_databricks", fake_execute_databricks)
    config = runtime_config()
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    principal = Principal(principal_id="alice", tenant_id="chinook")
    result = execute_compiled_query(
        compiled,
        config,
        question="q",
        principal=principal,
        options=ExecuteOptions(audit_path=Path("/tmp/audit.jsonl")),
    )
    assert result.audit.backend == "databricks"
    assert result.audit.principal_id == "alice"
    assert called["kwargs"]["access_token"] == "pat"


def test_named_domain_gets_backend_dialect_and_file_domain_is_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A domain catalog must compile for the configured backend, not DuckDB."""
    from secure_query.demo.chinook import sample_catalog

    path = tmp_path / "catalog.json"
    path.write_text(sample_catalog().model_copy(update={"tenant_id": "file"}).model_dump_json())
    monkeypatch.setenv("SECURE_QUERY_CATALOG_FILE", str(path))
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "postgres")
    monkeypatch.delenv("SECURE_QUERY_DOMAIN", raising=False)

    assert load_active_catalog().tenant_id == "file"
    chinook = load_active_catalog("chinook")
    assert chinook.tenant_id == "chinook" and chinook.sql_dialect == "postgres"
