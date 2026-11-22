"""Runtime catalog load and execute backend dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from secure_query.auth import Principal
from secure_query.engine.execute import ExecuteOptions
from secure_query.engine.runtime import (
    execute_compiled_query,
    load_active_catalog,
    runtime_config,
)
from secure_query.kernel.compile import CompiledQuery


def test_backend_is_duckdb_unless_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks token in the environment does not move the sample's questions to a warehouse."""
    monkeypatch.delenv("SECURE_QUERY_BACKEND", raising=False)
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    assert runtime_config().backend == "duckdb"


def test_sample_catalog_refused_on_a_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "databricks")
    monkeypatch.delenv("SECURE_QUERY_CATALOG_FILE", raising=False)
    monkeypatch.delenv("SECURE_QUERY_DOMAINS", raising=False)
    with pytest.raises(RuntimeError, match="Chinook sample"):
        runtime_config()


def test_unknown_backend_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "snowflake")
    with pytest.raises(RuntimeError, match="expected duckdb"):
        runtime_config()


def test_load_active_catalog_defaults_to_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURE_QUERY_CATALOG_FILE", raising=False)
    catalog = load_active_catalog()
    assert catalog.tenant_id == "chinook"
    assert any(t.name == "Invoice" for t in catalog.tables)


def _file_catalog(tmp_path: Path) -> Path:
    from secure_query.demo.chinook import sample_catalog

    path = tmp_path / "catalog.json"
    path.write_text(sample_catalog().model_copy(update={"tenant_id": "file"}).model_dump_json())
    return path


def test_execute_compiled_query_uses_databricks_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_SCHEMA", "main.sales")
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "databricks")
    monkeypatch.setenv("SECURE_QUERY_CATALOG_FILE", str(_file_catalog(tmp_path)))

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
    assert called["kwargs"]["principal"] is principal  # routed with the caller, settings read inside


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

    catalog = load_active_catalog()
    assert catalog.tenant_id == "file" and catalog.sql_dialect == "postgres"
    with pytest.raises(RuntimeError, match="Chinook sample"):
        load_active_catalog("chinook")
