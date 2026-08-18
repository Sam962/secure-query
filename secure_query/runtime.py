"""Runtime wiring: catalog source, execute backend, audit path.

DuckDB is the local default. When DATABRICKS_HOST, DATABRICKS_HTTP_PATH, and
DATABRICKS_TOKEN are all set, execute routes to the SQL warehouse instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from secure_query.auth import Principal
from secure_query.catalog import Catalog
from secure_query.compile import CompiledQuery
from secure_query.databricks import catalog_json_path, databricks_settings_from_env, execute_databricks
from secure_query.examples.load_sample_db import DUCKDB_PATH
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.execute import ExecuteOptions, ExecutionError, ExecutionResult, execute_duckdb
from secure_query.logical_plan import LogicalPlan

ExecuteBackend = Literal["duckdb", "databricks"]


@dataclass(frozen=True)
class RuntimeConfig:
    backend: ExecuteBackend
    catalog: Catalog
    audit_path: Path
    duckdb_path: Path


def default_audit_path() -> Path:
    raw = (os.environ.get("SECURE_QUERY_AUDIT_PATH") or "data/audit.jsonl").strip()
    return Path(raw)


def databricks_configured() -> bool:
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    return bool(host and http_path and token)


def load_active_catalog() -> Catalog:
    """Approved catalog: JSON file if set, else Chinook sample."""
    path = (os.environ.get("SECURE_QUERY_CATALOG_FILE") or "").strip()
    if path:
        return catalog_json_path(path)
    return sample_catalog()


def runtime_config() -> RuntimeConfig:
    backend: ExecuteBackend = "databricks" if databricks_configured() else "duckdb"
    return RuntimeConfig(
        backend=backend,
        catalog=load_active_catalog(),
        audit_path=default_audit_path(),
        duckdb_path=Path(DUCKDB_PATH),
    )


def ensure_execute_ready(config: RuntimeConfig) -> None:
    """Raise ExecutionError when the chosen backend is not runnable."""
    if config.backend == "duckdb" and not config.duckdb_path.exists():
        raise ExecutionError(
            f"DuckDB sample database not found at {config.duckdb_path}; "
            "run: python -m secure_query.examples.load_sample_db"
        )
    if config.backend == "databricks":
        databricks_settings_from_env()


def execute_compiled_query(
    compiled: CompiledQuery,
    config: RuntimeConfig,
    *,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
) -> ExecutionResult:
    """Run compiled SQL on the configured backend."""
    opts = options or ExecuteOptions(audit_path=config.audit_path)
    if opts.audit_path is None:
        opts = ExecuteOptions(
            timeout_seconds=opts.timeout_seconds,
            max_rows=opts.max_rows,
            read_only=opts.read_only,
            audit_path=config.audit_path,
        )
    ensure_execute_ready(config)
    if config.backend == "databricks":
        settings = databricks_settings_from_env()
        return execute_databricks(
            compiled,
            host=settings["host"],
            http_path=settings["http_path"],
            access_token=settings["access_token"],
            plan=plan,
            question=question,
            principal=principal,
            options=opts,
        )
    return execute_duckdb(
        compiled,
        config.duckdb_path,
        plan=plan,
        question=question,
        principal=principal,
        options=opts,
    )
