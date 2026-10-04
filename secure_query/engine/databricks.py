"""Databricks warehouse execute + Unity Catalog catalog draft.

Execute uses a SELECT-only service principal / user token. Unity Catalog remains
the last line of defense (grants, RLS, column masks) even if a plan bug slips
through. Catalog drafts are generated from information_schema; a human still
approves the resulting Catalog before it is used as the security boundary.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Protocol

from secure_query.auth import Principal
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.kernel.compile import CompiledQuery
from secure_query.engine.execute import (
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    _make_audit,
    _maybe_write_audit,
)
from secure_query.kernel.logical_plan import LogicalPlan

_DTYPE_MAP: dict[str, str] = {
    "bigint": "int",
    "long": "int",
    "int": "int",
    "integer": "int",
    "smallint": "int",
    "tinyint": "int",
    "double": "float",
    "float": "float",
    "real": "float",
    "decimal": "float",
    "numeric": "float",
    "string": "str",
    "varchar": "str",
    "char": "str",
    "text": "str",
    "boolean": "bool",
    "bool": "bool",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "timestamp_ntz": "datetime",
    "date": "datetime",
    "json": "json",
}

_PII_TAGS = frozenset({"pii", "pii_high", "sensitive", "email", "phone", "ssn"})


class _SqlConnection(Protocol):
    def cursor(self) -> Any: ...
    def close(self) -> None: ...


def map_uc_dtype(data_type: str) -> str:
    """Map a Unity Catalog / Spark type name onto Catalog ColumnDtype."""
    raw = data_type.strip().lower()
    base = raw.split("(", 1)[0].strip()
    return _DTYPE_MAP.get(base, "str")


def pii_risk_from_tags(tags: list[str] | tuple[str, ...] | None) -> str:
    lowered = {t.strip().lower() for t in (tags or [])}
    if lowered & _PII_TAGS or any("pii" in t for t in lowered):
        return "high"
    return "none"


def draft_catalog(
    *,
    tenant_id: str,
    columns: list[dict[str, Any]],
    foreign_keys: list[dict[str, Any]] | None = None,
    display_columns: dict[str, str] | None = None,
    max_limit: int = 1000,
    require_limit: bool = True,
) -> Catalog:
    """Build a Catalog from information_schema-shaped rows (no live warehouse).

    `columns` items: table_name, column_name, data_type, optional tags (list[str]).
    `foreign_keys` items: left_table, left_column, right_table, right_column.
    """
    by_table: dict[str, list[ColumnSpec]] = {}
    for row in columns:
        table = str(row["table_name"])
        tags = row.get("tags") or []
        spec = ColumnSpec(
            name=str(row["column_name"]),
            dtype=map_uc_dtype(str(row["data_type"])),  # type: ignore[arg-type]
            pii_risk=pii_risk_from_tags(tags),  # type: ignore[arg-type]
        )
        by_table.setdefault(table, []).append(spec)

    display = display_columns or {}
    tables = [
        TableSpec(
            name=name,
            columns=cols,
            display_column=display.get(name),
        )
        for name, cols in by_table.items()
    ]
    join_keys = [
        JoinKey(
            left_table=str(fk["left_table"]),
            left_column=str(fk["left_column"]),
            right_table=str(fk["right_table"]),
            right_column=str(fk["right_column"]),
        )
        for fk in (foreign_keys or [])
    ]
    return Catalog(
        tenant_id=tenant_id,
        tables=tables,
        join_keys=join_keys,
        max_limit=max_limit,
        require_limit=require_limit,
        sql_dialect="databricks",
    )


def fetch_unity_catalog_draft(
    connection: Any,
    *,
    tenant_id: str,
    catalog_name: str,
    schema_name: str,
    max_limit: int = 1000,
) -> Catalog:
    """Query Unity Catalog information_schema and return an unapproved draft Catalog.

    The result is a draft: a data owner must review PII flags and join keys
    before this object is used as the planner allowlist.
    """
    cur = connection.cursor()
    cur.execute(
        """
        SELECT table_name, column_name, data_type
        FROM system.information_schema.columns
        WHERE table_catalog = :cat AND table_schema = :sch
        ORDER BY table_name, ordinal_position
        """,
        {"cat": catalog_name, "sch": schema_name},
    )
    col_rows = [
        {"table_name": r[0], "column_name": r[1], "data_type": r[2]}
        for r in cur.fetchall()
    ]
    try:
        cur.execute(
            """
            SELECT table_name, column_name, tag_name
            FROM system.information_schema.column_tags
            WHERE catalog_name = :cat AND schema_name = :sch
            """,
            {"cat": catalog_name, "sch": schema_name},
        )
        tags_by_col: dict[tuple[str, str], list[str]] = {}
        for table_name, column_name, tag_name in cur.fetchall():
            tags_by_col.setdefault((table_name, column_name), []).append(str(tag_name))
        for row in col_rows:
            row["tags"] = tags_by_col.get((row["table_name"], row["column_name"]), [])
    except Exception:  # noqa: BLE001 — tags are optional; draft still useful
        pass

    fks: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT
                kcu.table_name,
                kcu.column_name,
                kcu.referenced_table_name,
                kcu.referenced_column_name
            FROM system.information_schema.key_column_usage kcu
            WHERE kcu.table_catalog = :cat
              AND kcu.table_schema = :sch
              AND kcu.referenced_table_name IS NOT NULL
            """,
            {"cat": catalog_name, "sch": schema_name},
        )
        for left_table, left_col, right_table, right_col in cur.fetchall():
            fks.append(
                {
                    "left_table": left_table,
                    "left_column": left_col,
                    "right_table": right_table,
                    "right_column": right_col,
                }
            )
    except Exception:  # noqa: BLE001 — FKs optional
        pass

    return draft_catalog(
        tenant_id=tenant_id,
        columns=col_rows,
        foreign_keys=fks,
        max_limit=max_limit,
    )


def databricks_settings_from_env() -> dict[str, str]:
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    missing = [
        name
        for name, val in (
            ("DATABRICKS_HOST", host),
            ("DATABRICKS_HTTP_PATH", http_path),
            ("DATABRICKS_TOKEN", token),
        )
        if not val
    ]
    if missing:
        raise ExecutionError("missing Databricks settings: " + ", ".join(missing))
    return {"host": host.rstrip("/"), "http_path": http_path, "access_token": token}  # type: ignore[union-attr]


def execute_databricks(
    compiled: CompiledQuery,
    *,
    host: str | None = None,
    http_path: str | None = None,
    access_token: str | None = None,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
    connection: Any | None = None,
) -> ExecutionResult:
    """Run compiled SQL on a Databricks SQL warehouse (read-only grants assumed)."""
    opts = options or ExecuteOptions()
    if opts.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if opts.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    owns_connection = connection is None
    if connection is None:
        settings = {
            "host": host,
            "http_path": http_path,
            "access_token": access_token,
        }
        if not all(settings.values()):
            settings = databricks_settings_from_env()
        connection = _connect(
            host=str(settings["host"]),
            http_path=str(settings["http_path"]),
            access_token=str(settings["access_token"]),
        )

    started = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_execute_once, connection, compiled.sql, opts.max_rows)
    try:
        try:
            columns, rows, truncated = fut.result(timeout=opts.timeout_seconds)
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
                backend="databricks",
            )
            _maybe_write_audit(opts.audit_path, audit)
            raise ExecutionError(audit.error or "timeout") from exc
        except Exception as exc:  # noqa: BLE001
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
                backend="databricks",
            )
            _maybe_write_audit(opts.audit_path, audit)
            raise ExecutionError(str(exc)) from exc
    finally:
        pool.shutdown(wait=False)
        if owns_connection:
            _safe_close(connection)

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
        backend="databricks",
    )
    _maybe_write_audit(opts.audit_path, audit)
    return ExecutionResult(
        columns=columns,
        rows=rows,
        truncated=truncated,
        duration_ms=duration_ms,
        audit=audit,
        compiled=compiled,
    )


def _connect(*, host: str, http_path: str, access_token: str) -> Any:
    try:
        from databricks import sql as dbsql
    except ImportError as exc:
        raise ExecutionError(
            "Install the databricks extra: pip install -e '.[databricks]'"
        ) from exc
    hostname = host.replace("https://", "").replace("http://", "")
    return dbsql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=access_token,
    )


def _execute_once(
    connection: Any, sql: str, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        cols = [d[0] for d in (cursor.description or [])]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [tuple(r) for r in fetched[:max_rows]]
        return cols, rows, truncated
    finally:
        cursor.close()


def _safe_close(con: Any) -> None:
    try:
        con.close()
    except Exception:  # noqa: BLE001
        pass


def catalog_json_path(path: str | Path) -> Catalog:
    """Load an approved catalog JSON file (output of a reviewed UC draft)."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Catalog.model_validate(data)
