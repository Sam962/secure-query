"""Runtime wiring: catalog source, execute backend, audit path.

DuckDB is the local default. Databricks SQL warehouse when DATABRICKS_* are
set. Postgres when SECURE_QUERY_POSTGRES_DSN / DATABASE_URL is set.
SECURE_QUERY_BACKEND can force one of: duckdb | databricks | postgres.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from secure_query.auth import Principal
from secure_query.demo.load_chinook import DUCKDB_PATH
from secure_query.engine.databricks import databricks_settings_from_env, execute_databricks
from secure_query.engine.domains import load_domain_catalog
from secure_query.engine.execute import ExecuteOptions, ExecutionError, ExecutionResult, execute_duckdb
from secure_query.engine.postgres import execute_postgres, postgres_configured, postgres_dsn
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.logical_plan import LogicalPlan

ExecuteBackend = Literal["duckdb", "databricks", "postgres"]


@dataclass(frozen=True)
class RuntimeConfig:
    backend: ExecuteBackend
    catalog: Catalog
    audit_path: Path
    duckdb_path: Path
    postgres_dsn: str | None = None


def default_audit_path() -> Path:
    raw = (os.environ.get("SECURE_QUERY_AUDIT_PATH") or "data/audit.jsonl").strip()
    return Path(raw)


def databricks_configured() -> bool:
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    return bool(host and http_path and token)


def resolve_backend() -> ExecuteBackend:
    explicit = (os.environ.get("SECURE_QUERY_BACKEND") or "").strip().lower()
    if explicit in ("duckdb", "databricks", "postgres"):
        return explicit  # type: ignore[return-value]
    if databricks_configured():
        return "databricks"
    if postgres_configured():
        return "postgres"
    return "duckdb"


def backend_dialect(backend: ExecuteBackend | None = None) -> str | None:
    """sqlglot dialect the backend requires; None keeps the catalog's own (DuckDB)."""
    return {"postgres": "postgres", "databricks": "databricks"}.get(backend or resolve_backend())


def load_active_catalog(domain_id: str | None = None) -> Catalog:
    """Approved catalog for a domain (default: see engine.domains), ready for this backend."""
    return load_domain_catalog(domain_id, dialect=backend_dialect())[1]


def runtime_config() -> RuntimeConfig:
    backend = resolve_backend()
    return RuntimeConfig(
        backend=backend,
        catalog=load_active_catalog(),
        audit_path=default_audit_path(),
        duckdb_path=Path(DUCKDB_PATH),
        postgres_dsn=postgres_dsn() if backend == "postgres" else None,
    )


@lru_cache(maxsize=1)
def get_runtime() -> RuntimeConfig:
    """Process-wide runtime (catalog loaded and validated once). cache_clear() to reload."""
    return runtime_config()


def ensure_execute_ready(config: RuntimeConfig) -> None:
    """Raise ExecutionError when the chosen backend is not runnable."""
    if config.backend == "duckdb" and not config.duckdb_path.exists():
        raise ExecutionError(
            f"DuckDB sample database not found at {config.duckdb_path}; "
            "run: python -m secure_query.demo.load_chinook"
        )
    if config.backend == "databricks":
        databricks_settings_from_env()
    if config.backend == "postgres" and not (config.postgres_dsn or postgres_dsn()):
        raise ExecutionError("Postgres backend selected but no DSN is configured")


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
    if config.backend == "postgres":
        return execute_postgres(
            compiled,
            config.postgres_dsn,
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
