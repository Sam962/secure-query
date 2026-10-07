"""Execute compiled SQL with policy controls + audit stub (Phase 3).

Backend: DuckDB, read-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
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

log = logging.getLogger(__name__)

# After a timeout the query is interrupted, then the worker is given this long
# to stop so it is joined rather than leaked.
CANCEL_GRACE_SECONDS = 1.0


class ExecutionError(Exception):
    """Raised when execution fails (timeout, DB error, etc.).

    `audit_id` names the audit record holding the detail; clients get the id,
    never the database's error text.
    """

    def __init__(self, message: str, *, audit_id: str | None = None) -> None:
        super().__init__(message)
        self.audit_id = audit_id


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
    question_sha256: str | None
    """The question is not stored: users type anything into it."""
    question_length: int | None
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
    audit_id: str = ""

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


def run_with_policy(
    compiled: CompiledQuery,
    *,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
    backend: str = "duckdb",
    runner: Callable[[], tuple[list[str], list[tuple[Any, ...]], bool]],
) -> ExecutionResult:
    """Shared timeout/audit wrapper around a backend runner (S6)."""
    opts = options or ExecuteOptions()
    if opts.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if opts.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    started = time.perf_counter()
    try:
        columns, rows, truncated = runner()
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
            backend=backend,
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
            backend=backend,
        )
        _maybe_write_audit(opts.audit_path, audit)
        raise ExecutionError(audit.error or "timeout", audit_id=audit.audit_id) from exc
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
            backend=backend,
        )
        _maybe_write_audit(opts.audit_path, audit)
        raise ExecutionError(str(exc), audit_id=audit.audit_id) from exc

    _maybe_write_audit(opts.audit_path, audit)
    return ExecutionResult(
        columns=columns,
        rows=rows,
        truncated=truncated,
        duration_ms=duration_ms,
        audit=audit,
        compiled=compiled,
    )


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
    path = Path(db_path)
    if not path.exists():
        raise ExecutionError(f"Database not found: {path}")
    opts = options or ExecuteOptions()
    return run_with_policy(
        compiled,
        plan=plan,
        question=question,
        principal=principal,
        options=opts,
        backend="duckdb",
        runner=lambda: _run_with_timeout(
            path,
            compiled.sql,
            timeout_seconds=opts.timeout_seconds,
            max_rows=opts.max_rows,
            read_only=opts.read_only,
        ),
    )


def _run_with_timeout(
    path: Path,
    sql: str,
    *,
    timeout_seconds: float,
    max_rows: int,
    read_only: bool,
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Run `sql`, returning shortly after `timeout_seconds` even if the query overruns.

    On timeout the connection is interrupted, which stops a DuckDB query within
    milliseconds, and the worker is joined (up to CANCEL_GRACE_SECONDS) before the
    connection is closed, so neither the thread nor the query is left running.
    """
    import duckdb

    con = duckdb.connect(str(path), read_only=read_only)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sq-duckdb")
    fut = pool.submit(_execute_once, con, sql, max_rows)
    try:
        return fut.result(timeout=timeout_seconds)
    except FuturesTimeout:
        con.interrupt()
        _await_stop(fut, "duckdb")
        raise
    finally:
        pool.shutdown(wait=fut.done())
        if fut.done():
            _safe_close(con)
        else:
            fut.add_done_callback(lambda _f: _safe_close(con))


def _await_stop(fut: Any, backend: str) -> None:
    """Give a cancelled query CANCEL_GRACE_SECONDS to stop; log if it does not."""
    try:
        fut.result(timeout=CANCEL_GRACE_SECONDS)
    except FuturesTimeout:
        log.warning("%s query still running %.1fs after cancel", backend, CANCEL_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 — the interrupt error is expected
        pass


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
        question_sha256=hashlib.sha256(question.encode()).hexdigest() if question else None,
        question_length=len(question) if question else None,
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
        audit_id=uuid.uuid4().hex,
    )


def _maybe_write_audit(path: Path | None, audit: AuditRecord) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(audit.to_json() + "\n")
