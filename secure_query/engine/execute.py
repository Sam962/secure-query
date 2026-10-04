"""Execute compiled SQL with policy controls + audit stub (Phase 3).

Backend: DuckDB, read-only.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from secure_query.auth import Principal, audit_principal_fields
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan


class ExecutionError(Exception):
    """Raised when execution fails (timeout, DB error, etc.)."""


@dataclass(frozen=True)
class ExecuteOptions:
    """Policy knobs enforced at execute time (in addition to catalog validate)."""

    timeout_seconds: float = 30.0
    max_rows: int = 1000
    read_only: bool = True
    audit_path: Path | None = None  # JSONL append; None = no file write


@dataclass(frozen=True)
class AuditRecord:
    """Minimal audit row for replay / compliance."""

    timestamp: str
    status: str  # ok | timeout | error
    question: str | None
    plan_id: str | None
    plan_json: str | None
    explanation: str | None
    sql: str
    plan_hash: str
    sql_hash: str
    row_count: int
    truncated: bool
    duration_ms: float
    error: str | None = None
    backend: str = "duckdb"
    principal_id: str | None = None
    tenant_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ExecutionResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    duration_ms: float
    audit: AuditRecord
    compiled: CompiledQuery


def execute_duckdb(
    compiled: CompiledQuery,
    db_path: str | Path,
    *,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
) -> ExecutionResult:
    """Run compiled SQL on DuckDB with timeout + row cap + audit stub."""
    opts = options or ExecuteOptions()
    if opts.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if opts.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    path = Path(db_path)
    if not path.exists():
        raise ExecutionError(f"Database not found: {path}")

    started = time.perf_counter()
    try:
        columns, rows, truncated = _run_with_timeout(
            path,
            compiled.sql,
            timeout_seconds=opts.timeout_seconds,
            max_rows=opts.max_rows,
            read_only=opts.read_only,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        audit = _make_audit(
            status="ok",
            compiled=compiled,
            plan=plan,
            question=question,
            principal=principal,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=duration_ms,
        )
    except FuturesTimeout as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        audit = _make_audit(
            status="timeout",
            compiled=compiled,
            plan=plan,
            question=question,
            principal=principal,
            row_count=0,
            truncated=False,
            duration_ms=duration_ms,
            error=f"query exceeded timeout_seconds={opts.timeout_seconds}",
        )
        _maybe_write_audit(opts.audit_path, audit)
        raise ExecutionError(audit.error or "timeout") from exc
    except Exception as exc:  # noqa: BLE001 — record then re-raise as ExecutionError
        duration_ms = (time.perf_counter() - started) * 1000
        if isinstance(exc, ExecutionError):
            raise
        audit = _make_audit(
            status="error",
            compiled=compiled,
            plan=plan,
            question=question,
            principal=principal,
            row_count=0,
            truncated=False,
            duration_ms=duration_ms,
            error=str(exc),
        )
        _maybe_write_audit(opts.audit_path, audit)
        raise ExecutionError(str(exc)) from exc

    _maybe_write_audit(opts.audit_path, audit)
    return ExecutionResult(
        columns=columns,
        rows=rows,
        truncated=truncated,
        duration_ms=duration_ms,
        audit=audit,
        compiled=compiled,
    )


def _run_with_timeout(
    path: Path,
    sql: str,
    *,
    timeout_seconds: float,
    max_rows: int,
    read_only: bool,
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Run `sql`, returning within `timeout_seconds` even if the query overruns.

    On timeout the DuckDB connection is interrupted so the query stops consuming
    resources, and the caller is never blocked waiting for the worker thread.
    """
    import duckdb

    con = duckdb.connect(str(path), read_only=read_only)
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_execute_once, con, sql, max_rows)
    try:
        return fut.result(timeout=timeout_seconds)
    except FuturesTimeout:
        con.interrupt()
        raise
    finally:
        # wait=False: a runaway query must not extend the caller's wall clock.
        pool.shutdown(wait=False)
        if fut.done():
            _safe_close(con)
        else:
            fut.add_done_callback(lambda _f: _safe_close(con))


def _safe_close(con: Any) -> None:
    try:
        con.close()
    except Exception:  # noqa: BLE001 — closing must never mask the real error
        pass


def _execute_once(
    con: Any, sql: str, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    result = con.execute(sql)
    cols = [d[0] for d in result.description] if result.description else []
    # Fetch one extra row to detect truncation.
    fetched = result.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = fetched[:max_rows]
    return cols, rows, truncated


def _make_audit(
    *,
    status: str,
    compiled: CompiledQuery,
    plan: LogicalPlan | None,
    question: str | None,
    row_count: int,
    truncated: bool,
    duration_ms: float,
    error: str | None = None,
    principal: Principal | None = None,
    backend: str = "duckdb",
) -> AuditRecord:
    who = audit_principal_fields(principal)
    return AuditRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        question=question,
        plan_id=str(plan.plan_id) if plan is not None else None,
        plan_json=plan.to_json() if plan is not None else None,
        explanation=explain_plan(plan) if plan is not None else None,
        sql=compiled.sql,
        plan_hash=compiled.plan_hash,
        sql_hash=compiled.sql_hash,
        row_count=row_count,
        truncated=truncated,
        duration_ms=round(duration_ms, 3),
        error=error,
        backend=backend,
        principal_id=who["principal_id"],
        tenant_id=who["tenant_id"],
    )


def _maybe_write_audit(path: Path | None, audit: AuditRecord) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(audit.to_json() + "\n")
