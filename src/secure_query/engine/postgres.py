"""Postgres execute path — same policy knobs as DuckDB (timeout, row cap, audit)."""

from __future__ import annotations

import os
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any
from urllib.parse import urlparse, urlunparse

from secure_query.auth import Principal
from secure_query.engine.execute import (
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    _safe_close,
    run_with_policy,
)
from secure_query.kernel.compile import CompiledQuery
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

    return run_with_policy(
        compiled,
        plan=plan,
        question=question,
        principal=principal,
        options=opts,
        backend="postgres",
        runner=lambda: _run_with_timeout(
            target,
            compiled.sql,
            timeout_seconds=opts.timeout_seconds,
            max_rows=opts.max_rows,
        ),
    )


def _run_with_timeout(
    dsn: str,
    sql: str,
    *,
    timeout_seconds: float,
    max_rows: int,
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Run `sql` in a read-only session; the server enforces the timeout.

    `default_transaction_read_only` makes Postgres itself refuse writes, so the
    SQL validator is not the only barrier. `statement_timeout` cancels the query
    server-side, so no worker thread is needed and none can be left running.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise ExecutionError(
            "Install the postgres extra: pip install -e '.[postgres]'"
        ) from exc

    timeout_ms = max(1, int(timeout_seconds * 1000))
    conn = psycopg.connect(
        _normalize_dsn(dsn), autocommit=True, connect_timeout=max(1, int(timeout_seconds))
    )
    try:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute(f"SET statement_timeout = {timeout_ms}")
        try:
            return _execute_once(conn, sql, max_rows)
        except psycopg.errors.QueryCanceled as exc:
            raise FuturesTimeout(str(exc)) from exc
    finally:
        _safe_close(conn)


def _execute_once(
    conn: Any, sql: str, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    result = conn.execute(sql)
    cols = [d[0] for d in result.description] if result.description else []
    fetched = result.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [tuple(row) for row in fetched[:max_rows]]
    return cols, rows, truncated


def _normalize_dsn(dsn: str) -> str:
    """Accept SQLAlchemy-style postgresql+psycopg:// URLs."""
    if "://" not in dsn:
        return dsn
    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme in {"postgres", "postgresql"}:
        return urlunparse(parsed._replace(scheme="postgresql"))
    return dsn
