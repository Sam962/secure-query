"""Validate model-written SQL against the approved catalog (the SQL planner path).

The model's SQL is never executed as written. It is parsed into an AST,
every column is resolved against the catalog schema (sqlglot `qualify`), and
the resolved tree must satisfy the same policy the LogicalPlan path enforces:

  - exactly one read-only query (SELECT / UNION / INTERSECT / EXCEPT, CTEs allowed)
  - sources are catalog tables or CTEs/derived tables; no table functions
  - every column resolves to an approved catalog column; none is pii=high
  - every join is an equi-join on an approved join key; no cross joins
  - only known SQL functions (unknown names such as getenv are rejected)
  - an outer LIMIT no larger than the catalog maximum

The SQL that runs is regenerated from the validated tree, never the model text.

Not yet enforced on this path: principal row filters (LogicalPlan path only)
and fan-out detection. Both are required before production use.
"""

from __future__ import annotations

import hashlib

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.errors import ValidationError

_STATEMENTS = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command",
        "Copy", "Pragma", "Set", "Use", "Transaction", "Commit", "Rollback", "LoadData",
        "Attach", "Detach", "Install", "Grant", "Revoke", "Cache", "Uncache", "Refresh",
        "Analyze", "Export", "Describe", "Summarize",
    )
    if hasattr(exp, name)
)
# Functions sqlglot does not model as typed nodes. Anything else unknown is rejected.
_SAFE_ANONYMOUS = frozenset({"strftime", "strptime", "date_part", "datediff", "date_diff"})


class SqlValidationFailed(Exception):
    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.code}: {e.message}" for e in errors))


def _error(code: str, message: str) -> ValidationError:
    return ValidationError(code=code, path="$.sql", message=message, stage="policy")


def validate_sql(sql: str, catalog: Catalog) -> CompiledQuery:
    """Return the compiled, policy-checked query or raise SqlValidationFailed."""
    dialect = "duckdb"  # the model is prompted for DuckDB SQL; output uses catalog.sql_dialect
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:  # ParseError, TokenError, …
        raise SqlValidationFailed([_error("sql.parse", str(exc).splitlines()[0])]) from exc
    if len(statements) != 1:
        raise SqlValidationFailed([_error("sql.statements", "exactly one statement is allowed")])
    tree = statements[0]
    if not isinstance(tree, exp.Query) or any(isinstance(n, _STATEMENTS) for n in tree.walk()):
        raise SqlValidationFailed([_error("sql.read_only", "only a read-only SELECT is allowed")])

    tables = {t.name.lower(): t for t in catalog.tables}
    schema = {t.name: {c.name: c.dtype for c in t.columns} for t in catalog.tables}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlValidationFailed([_error("sql.table_function", "table functions are not allowed")])
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlValidationFailed([_error("sql.unknown_table", f"{table.sql()} is not a catalog table")])
        if table.name.lower() not in tables and table.name.lower() not in ctes:
            raise SqlValidationFailed([_error("sql.unknown_table", f"{table.name} is not in the catalog")])

    try:
        tree = qualify(
            tree, schema=schema, dialect=dialect, validate_qualify_columns=True, identify=True
        )
    except (OptimizeError, SqlglotError) as exc:
        raise SqlValidationFailed([_error("sql.unknown_column", str(exc).splitlines()[0])]) from exc

    errors: list[ValidationError] = []
    for func in tree.find_all(exp.Anonymous):
        if str(func.this).lower() not in _SAFE_ANONYMOUS:
            errors.append(_error("sql.function", f"function {func.this} is not allowed"))

    for scope in traverse_scope(tree):
        base = {
            alias: tables[src.name.lower()]
            for alias, src in scope.sources.items()
            if isinstance(src, exp.Table) and src.name.lower() in tables
        }
        for column in scope.columns:
            spec_table = base.get(column.table)
            if spec_table is None:
                continue  # resolves to a CTE / derived table, validated in its own scope
            cols = {c.name.lower(): c for c in spec_table.columns}
            spec = cols.get(column.name.lower())
            if spec is None:
                errors.append(_error("sql.unknown_column", f"{spec_table.name}.{column.name}"))
            elif spec.pii_risk == "high":
                errors.append(
                    _error("sql.pii", f"{spec_table.name}.{spec.name} is restricted (pii=high)")
                )
        errors.extend(_check_joins(scope, base, catalog))
        errors.extend(_check_unused_joins(scope))
    if errors:
        raise SqlValidationFailed(errors)

    _enforce_limit(tree, catalog)
    out = tree.sql(dialect=catalog.sql_dialect)
    digest = hashlib.sha256(out.encode()).hexdigest()
    return CompiledQuery(sql=out, plan_hash=f"sql:{digest[:16]}", sql_hash=digest, parameters=[])


def _check_joins(scope, base: dict, catalog: Catalog) -> list[ValidationError]:
    select = scope.expression
    if not isinstance(select, exp.Select):
        return []
    errors: list[ValidationError] = []
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is None:
            if join.args.get("using"):
                errors.append(_error("sql.join", "JOIN ... USING is not allowed; use ON a.key = b.key"))
            else:
                errors.append(_error("sql.cross_join", "cross joins are not allowed"))
            continue
        approved = False
        for eq in on.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            lt, rt = base.get(left.table), base.get(right.table)
            if lt is None or rt is None:
                approved = True  # a derived table / CTE side: its columns were validated
                continue
            if catalog.allow_any_join or _join_allowed(catalog, lt.name, left.name, rt.name, right.name):
                approved = True
            else:
                errors.append(
                    _error(
                        "sql.join_not_allowed",
                        f"{lt.name}.{left.name} = {rt.name}.{right.name} is not an approved join key",
                    )
                )
        if not approved and not errors:
            errors.append(_error("sql.join", "each JOIN needs an equality on an approved join key"))
    return errors


def _check_unused_joins(scope) -> list[ValidationError]:
    """A joined table used only in its ON clause repeats or drops rows silently.

    Allowed when the query deduplicates or aggregates (DISTINCT, GROUP BY,
    aggregates), where the join can only act as an existence filter.
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or not select.args.get("joins"):
        return []
    if select.args.get("distinct") or select.args.get("group") or select.find(exp.AggFunc):
        return []
    on_columns = {
        id(col) for join in select.args["joins"] for col in (join.args.get("on") or exp.null()).find_all(exp.Column)
    }
    used = {col.table for col in scope.columns if id(col) not in on_columns}
    errors = []
    for join in select.args["joins"]:
        alias = join.this.alias_or_name
        if alias not in used:
            errors.append(
                _error(
                    "sql.unused_join",
                    f"{join.this.name} is joined but none of its columns are used, which "
                    "repeats or drops rows; remove the join, or use EXISTS / DISTINCT if it "
                    "is meant as a filter",
                )
            )
    return errors


def _join_allowed(catalog: Catalog, lt: str, lc: str, rt: str, rc: str) -> bool:
    key = {(lt.lower(), lc.lower()), (rt.lower(), rc.lower())}
    return any(
        {(jk.left_table.lower(), jk.left_column.lower()), (jk.right_table.lower(), jk.right_column.lower())}
        == key
        for jk in catalog.join_keys
    )


def _enforce_limit(tree: exp.Query, catalog: Catalog) -> None:
    limit = tree.args.get("limit")
    value = None
    if limit is not None:
        node = limit.expression if isinstance(limit, exp.Limit) else limit
        if isinstance(node, exp.Literal) and node.is_int:
            value = int(node.this)
    if value is None and not catalog.require_limit:
        return
    if value is None or value > catalog.max_limit:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(min(value or catalog.max_limit, catalog.max_limit))))
