"""Postgres execute path — same policy knobs as DuckDB (timeout, row cap, audit)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any
from urllib.parse import urlparse, urlunparse

from secure_query.auth import Principal
from secure_query.kernel.compile import CompiledQuery
from secure_query.engine.execute import (
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    _make_audit,
    _maybe_write_audit,
)
from secure_query.kernel.logical_plan import LogicalPlan


def postgres_dsn() -> str | None:
    raw = (
        os.environ.get("SECURE_QUERY_POSTGRES_DSN")
        or os.environ.get("POSTGRES_DSN")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    return raw or None


def postgres_configured() -> bool:
    return bool(postgres_dsn())


def execute_postgres(
    compiled: CompiledQuery,
    dsn: str | None = None,
    *,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
) -> ExecutionResult:
    """Run compiled SQL on Postgres with timeout + row cap + audit."""
    opts = options or ExecuteOptions()
    if opts.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if opts.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    target = (dsn or postgres_dsn() or "").strip()
    if not target:
        raise ExecutionError("Postgres DSN not configured")

    started = time.perf_counter()
    try:
        columns, rows, truncated = _run_with_timeout(
            target,
            compiled.sql,
            timeout_seconds=opts.timeout_seconds,
            max_rows=opts.max_rows,
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
            backend="postgres",
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
            backend="postgres",
        )
        _maybe_write_audit(opts.audit_path, audit)
        raise ExecutionError(audit.error or "timeout") from exc
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ExecutionError):
            raise
        duration_ms = (time.perf_counter() - started) * 1000
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
            backend="postgres",
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
    dsn: str,
    sql: str,
    *,
    timeout_seconds: float,
    max_rows: int,
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    try:
        import psycopg
    except ImportError as exc:
        raise ExecutionError(
            "Install the postgres extra: pip install -e '.[postgres]'"
        ) from exc

    timeout_ms = max(1, int(timeout_seconds * 1000))
    conn = psycopg.connect(_normalize_dsn(dsn), autocommit=True)
    try:
        conn.execute(f"SET statement_timeout = {timeout_ms}")
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(_execute_once, conn, sql, max_rows)
        try:
            return fut.result(timeout=timeout_seconds)
        except FuturesTimeout:
            try:
                conn.cancel()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            pool.shutdown(wait=False)
            if fut.done():
                _safe_close(conn)
            else:
                fut.add_done_callback(lambda _f: _safe_close(conn))
    except Exception:
        if not conn.closed:
            _safe_close(conn)
        raise


def _execute_once(
    conn: Any, sql: str, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    result = conn.execute(sql)
    cols = [d[0] for d in result.description] if result.description else []
    fetched = result.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [tuple(row) for row in fetched[:max_rows]]
    return cols, rows, truncated


def _safe_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _normalize_dsn(dsn: str) -> str:
    """Accept SQLAlchemy-style postgresql+psycopg:// URLs."""
    if "://" not in dsn:
        return dsn
    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme in {"postgres", "postgresql"}:
        return urlunparse(parsed._replace(scheme="postgresql"))
    return dsn
