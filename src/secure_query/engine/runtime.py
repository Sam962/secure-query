"""Runtime wiring: catalog source, execute backend, audit path.

SECURE_QUERY_BACKEND names the backend: duckdb (default) | databricks | postgres. It is
never inferred from which credentials happen to be set: a domain's catalog is approved for
one warehouse, and a Databricks token in the environment must not send its questions to
another (engine.domains refuses the Chinook sample on any backend but DuckDB).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from secure_query.auth import Principal
from secure_query.engine.databricks import databricks_settings_from_env, execute_databricks
from secure_query.engine.domains import load_domain_catalog
from secure_query.engine.execute import ExecuteOptions, ExecutionError, ExecutionResult, execute_duckdb
from secure_query.engine.postgres import execute_postgres, postgres_dsn
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


def resolve_backend() -> ExecuteBackend:
    explicit = (os.environ.get("SECURE_QUERY_BACKEND") or "duckdb").strip().lower()
    if explicit not in ("duckdb", "databricks", "postgres"):
        raise RuntimeError(f"SECURE_QUERY_BACKEND={explicit!r}: expected duckdb, databricks or postgres")
    return explicit  # type: ignore[return-value]


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
        duckdb_path=Path(os.environ.get("SECURE_QUERY_DUCKDB") or "data/chinook.duckdb"),
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
        # Host/path/schema must be set; end-user token is resolved per request.
        databricks_settings_from_env(require_token=False)
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
        return execute_databricks(
            compiled,
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
