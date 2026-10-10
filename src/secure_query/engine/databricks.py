"""Databricks warehouse execute + Unity Catalog catalog draft.

Execute uses a SELECT-only service principal / user token. Unity Catalog remains
the last line of defense (grants, RLS, column masks) even if a plan bug slips
through. Catalog drafts are generated from information_schema; a human still
approves the resulting Catalog before it is used as the security boundary.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from secure_query.auth import Principal
from secure_query.engine.execute import (
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    _await_stop,
    _safe_close,
    run_with_policy,
)
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.kernel.compile import CompiledQuery
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
    """Connection settings, including the catalog and schema the approved tables live in.

    The validator only accepts unqualified table names, so the session's default
    catalog/schema decides which `Invoice` a query reads; it must be set explicitly.
    """
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    catalog, _, schema = (os.environ.get("SECURE_QUERY_DATABRICKS_SCHEMA") or "").strip().partition(".")
    missing = [
        name
        for name, val in (
            ("DATABRICKS_HOST", host),
            ("DATABRICKS_HTTP_PATH", http_path),
            ("DATABRICKS_TOKEN", token),
            ("SECURE_QUERY_DATABRICKS_SCHEMA (catalog.schema)", catalog and schema),
        )
        if not val
    ]
    if missing:
        raise ExecutionError("missing Databricks settings: " + ", ".join(missing))
    return {
        "host": host.rstrip("/"),  # type: ignore[union-attr]
        "http_path": http_path,  # type: ignore[dict-item]
        "access_token": token,  # type: ignore[dict-item]
        "catalog": catalog,
        "schema": schema,
    }


def execute_databricks(
    compiled: CompiledQuery,
    *,
    plan: LogicalPlan | None = None,
    question: str | None = None,
    principal: Principal | None = None,
    options: ExecuteOptions | None = None,
    connection: Any | None = None,
) -> ExecutionResult:
    """Run compiled SQL on a Databricks SQL warehouse.

    The warehouse principal must hold SELECT-only privileges (USE CATALOG / USE
    SCHEMA / SELECT); `databricks_grant_check` verifies that at /ready. On timeout
    the running statement is cancelled and the worker joined.
    """
    opts = options or ExecuteOptions()
    if opts.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if opts.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    owns_connection = connection is None
    if connection is None:
        connection = _connect(**databricks_settings_from_env())

    def _runner() -> tuple[list[str], list[tuple[Any, ...]], bool]:
        cursor = connection.cursor()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sq-databricks")
        fut = pool.submit(_execute_once, cursor, compiled.sql, opts.max_rows)
        try:
            try:
                return fut.result(timeout=opts.timeout_seconds)
            except FuturesTimeout:
                cancel = getattr(cursor, "cancel", None)
                if cancel is not None:
                    try:
                        cancel()
                    except Exception:  # noqa: BLE001 — the timeout is still reported
                        pass
                _await_stop(fut, "databricks")
                raise
        finally:
            pool.shutdown(wait=fut.done())
            _safe_close(cursor)
            if owns_connection:
                _safe_close(connection)

    return run_with_policy(
        compiled,
        plan=plan,
        question=question,
        principal=principal,
        options=opts,
        backend="databricks",
        runner=_runner,
    )


def _connect(*, host: str, http_path: str, access_token: str, catalog: str, schema: str) -> Any:
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
        catalog=catalog,
        schema=schema,
    )


def _execute_once(
    cursor: Any, sql: str, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    cursor.execute(sql)
    cols = [d[0] for d in (cursor.description or [])]
    fetched = cursor.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [tuple(r) for r in fetched[:max_rows]]
    return cols, rows, truncated


# Privileges that let the warehouse principal change data or metadata.
_WRITE_PRIVILEGES = frozenset(
    {
        "ALL PRIVILEGES", "ALL_PRIVILEGES", "MODIFY", "CREATE", "CREATE TABLE", "CREATE_TABLE",
        "CREATE SCHEMA", "CREATE_SCHEMA", "CREATE FUNCTION", "CREATE_FUNCTION",
        "CREATE VOLUME", "CREATE_VOLUME", "CREATE MATERIALIZED VIEW", "CREATE_MATERIALIZED_VIEW",
        "WRITE VOLUME", "WRITE_VOLUME", "WRITE FILES", "WRITE_FILES", "APPLY TAG", "APPLY_TAG",
        "MANAGE", "OWNERSHIP", "OWN",
    }
)


def databricks_grant_check(connection: Any | None = None) -> dict[str, str]:
    """Is the warehouse principal SELECT-only on SECURE_QUERY_DATABRICKS_SCHEMA?

    Returns {"status": "select_only" | "write_privileges" | "unchecked", "detail"}.
    Only direct grants to the current user are visible to SHOW GRANTS; grants
    through groups are not, so "select_only" is a necessary check, not a proof.
    """
    schema = (os.environ.get("SECURE_QUERY_DATABRICKS_SCHEMA") or "").strip()
    if not schema:
        return {"status": "unchecked", "detail": "SECURE_QUERY_DATABRICKS_SCHEMA not set"}
    owns = connection is None
    if connection is None:
        connection = _connect(**databricks_settings_from_env())
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT current_user()")
        user = str(cursor.fetchone()[0])
        quoted = ".".join(f"`{part.replace('`', '``')}`" for part in schema.split("."))
        cursor.execute(f"SHOW GRANTS ON SCHEMA {quoted}")
        cols = [d[0].lower() for d in (cursor.description or [])]
        grants = [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]
    finally:
        _safe_close(cursor)
        if owns:
            _safe_close(connection)
    mine = {
        str(g.get("actiontype") or g.get("privilege") or "").upper()
        for g in grants
        if str(g.get("principal") or "") == user
    }
    write = sorted(mine & _WRITE_PRIVILEGES)
    if write:
        return {"status": "write_privileges", "detail": f"{user} has {', '.join(write)} on {schema}"}
    return {"status": "select_only", "detail": f"{user}: {', '.join(sorted(mine)) or 'no direct grants'}"}
