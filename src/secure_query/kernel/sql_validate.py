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

Also enforced, as on the LogicalPlan path: aggregates over rows a one-to-many
join repeats are rejected (fan-out), and principal row filters are injected by
replacing each filtered base table with a filtered subquery, so no part of the
query can see unfiltered rows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery, compile_filter
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.joins import approved_path
from secure_query.kernel.logical_plan import Filter

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


@dataclass(frozen=True)
class ValidatedSql:
    """A policy-checked query plus what it reads, for semantic guards and audit."""

    compiled: CompiledQuery
    columns: frozenset[tuple[str, str]]
    """(table, column) for every catalog column the query reads, canonical names."""
    tables: frozenset[str]
    filter_strings: tuple[str, ...]
    """String literals in WHERE / HAVING / ON (values the query filters on)."""
    filter_numbers: tuple[float, ...]
    aggregates: frozenset[str]
    """Lower-case aggregate names used (count, sum, avg, min, max)."""


class SqlValidationFailed(Exception):
    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.code}: {e.message}" for e in errors))


def _error(code: str, message: str) -> ValidationError:
    return ValidationError(code=code, path="$.sql", message=message, stage="policy")


def validate_sql(
    sql: str, catalog: Catalog, *, row_filters: Sequence[Filter] = ()
) -> ValidatedSql:
    """Return the policy-checked query (with row filters applied) or raise SqlValidationFailed."""
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

    _restore_case(tree, tables)
    errors: list[ValidationError] = []
    used: set[tuple[str, str]] = set()
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
            else:
                used.add((spec_table.name, spec.name))
        errors.extend(_check_joins(scope, base, catalog))
        errors.extend(_check_unused_joins(scope))
        errors.extend(_check_fan_out(scope, base, catalog))
    if errors:
        raise SqlValidationFailed(errors)

    strings, numbers = _filter_literals(tree)
    aggregates = frozenset(
        type(a).__name__.lower() for a in tree.find_all(exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
    )
    read_tables = frozenset(
        tables[t.name.lower()].name for t in tree.find_all(exp.Table) if t.name.lower() in tables
    )
    _inject_row_filters(tree, tables, row_filters, catalog)
    _enforce_limit(tree, catalog)
    out = tree.sql(dialect=catalog.sql_dialect)
    digest = hashlib.sha256(out.encode()).hexdigest()
    return ValidatedSql(
        compiled=CompiledQuery(sql=out, plan_hash=f"sql:{digest[:16]}", sql_hash=digest, parameters=[]),
        columns=frozenset(used),
        tables=read_tables,
        filter_strings=strings,
        filter_numbers=numbers,
        aggregates=aggregates,
    )


def _restore_case(tree: exp.Expression, tables: dict) -> None:
    """qualify lower-cases identifiers; put back catalog spelling (case-sensitive backends)."""
    columns = {
        c.name.lower(): c.name for t in tables.values() for c in t.columns
    }
    for table in tree.find_all(exp.Table):
        spec = tables.get(table.name.lower())
        if spec is not None:
            table.set("this", exp.to_identifier(spec.name, quoted=True))
    for column in tree.find_all(exp.Column):
        canonical = columns.get(column.name.lower())
        if canonical is not None:
            column.set("this", exp.to_identifier(canonical, quoted=True))


def _filter_literals(tree: exp.Expression) -> tuple[tuple[str, ...], tuple[float, ...]]:
    strings: list[str] = []
    numbers: list[float] = []
    for clause in tree.find_all(exp.Where, exp.Having, exp.Join):
        for lit in clause.find_all(exp.Literal):
            if lit.is_string:
                strings.append(lit.this)
            elif lit.is_number:
                numbers.append(float(lit.this))
    return tuple(strings), tuple(numbers)


def _inject_row_filters(
    tree: exp.Expression, tables: dict, row_filters: Sequence[Filter], catalog: Catalog
) -> None:
    """Restrict every base table the query reads to rows the principal may see.

    A filtered table X becomes (SELECT * FROM X WHERE <filters>). Any other
    table T is restricted through its approved join path to X with an EXISTS
    semi-join (no row duplication in either direction), the same rows the
    LogicalPlan path reaches by auto-joining X. A table with no approved path
    to X cannot be filtered and is rejected.
    """
    by_table: dict[str, list[Filter]] = {}
    for filt in row_filters:
        canonical = tables[filt.column.table_id.lower()].name
        by_table.setdefault(canonical, []).append(filt)
    if not by_table:
        return
    errors: list[ValidationError] = []
    for table in list(tree.find_all(exp.Table)):
        spec = tables.get(table.name.lower())
        if spec is None:
            continue  # CTE reference: its body is rewritten through its own tables
        outer = "_rf_row"
        inner = exp.select("*").from_(
            exp.Table(this=exp.to_identifier(spec.name, quoted=True), alias=exp.TableAlias(this=exp.to_identifier(outer, quoted=True)))
        )
        for target, filters in by_table.items():
            if target == spec.name:
                for filt in filters:
                    inner = inner.where(_requalify(compile_filter(filt), spec.name, outer))
                continue
            try:
                path = approved_path(catalog, spec.name, target)
            except ValueError as exc:
                errors.append(_error("sql.row_filter_ambiguous", str(exc)))
                continue
            if path is None:
                errors.append(
                    _error(
                        "sql.row_filter_unreachable",
                        f"{spec.name} has no approved join path to {target}, so the "
                        "principal's row filter cannot be applied",
                    )
                )
                continue
            inner = inner.where(_exists_through(path, filters, outer))
        alias = table.alias or spec.name
        table.replace(
            exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
        )
    if errors:
        raise SqlValidationFailed(errors)


def _requalify(node: exp.Expression, table: str, alias: str) -> exp.Expression:
    for col in node.find_all(exp.Column):
        if col.table == table:
            col.set("table", exp.to_identifier(alias, quoted=True))
    return node


def _exists_through(path: list, filters: list[Filter], outer: str) -> exp.Expression:
    """EXISTS (SELECT 1 FROM t1 JOIN t2 ... WHERE t1.k = outer.k AND <filters>)."""

    def ident(name: str) -> exp.Identifier:
        return exp.to_identifier(name, quoted=True)

    first_from, first_to, first_key = path[0]
    from_col, to_col = (
        (first_key.left_column, first_key.right_column)
        if first_key.left_table == first_from
        else (first_key.right_column, first_key.left_column)
    )
    sub = exp.select(exp.Literal.number(1)).from_(exp.Table(this=ident(first_to)))
    sub = sub.where(
        exp.EQ(
            this=exp.Column(this=ident(to_col), table=ident(first_to)),
            expression=exp.Column(this=ident(from_col), table=ident(outer)),
        )
    )
    for prev, node, key in path[1:]:
        left_col, right_col = (
            (key.left_column, key.right_column) if key.left_table == prev else (key.right_column, key.left_column)
        )
        sub = sub.join(
            exp.Table(this=ident(node)),
            on=exp.EQ(
                this=exp.Column(this=ident(left_col), table=ident(prev)),
                expression=exp.Column(this=ident(right_col), table=ident(node)),
            ),
        )
    for filt in filters:
        sub = sub.where(compile_filter(filt))
    return exp.Exists(this=sub)


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


_FAN_OUT_SAFE = (exp.Min, exp.Max)


def _check_fan_out(scope, base: dict, catalog: Catalog) -> list[ValidationError]:
    """Aggregates over rows that a one-to-many join repeats are inflated.

    Mirrors kernel.joins.fan_out_errors: stepping from alias a to b is to-one
    when the ON equality follows a join key from its many side to its one side.
    An aggregate over alias T is exact only if every step outward from T is
    to-one; COUNT(*) needs some alias to be that grain. COUNT(DISTINCT), MIN
    and MAX are unaffected. Joins with a CTE / derived side are not checked.
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or not select.args.get("joins"):
        return []
    aggs = [a for a in select.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select]
    if not aggs:
        return []
    to_one: dict[str, dict[str, bool]] = {}
    for join in select.args["joins"]:
        on = join.args.get("on")
        for eq in on.find_all(exp.EQ) if on is not None else []:
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            lt, rt = base.get(left.table), base.get(right.table)
            if lt is None or rt is None:
                return []  # derived side: cardinality unknown, not checked
            forward = any(
                jk.left_table.lower() == lt.name.lower()
                and jk.left_column.lower() == left.name.lower()
                and jk.right_table.lower() == rt.name.lower()
                and jk.right_column.lower() == right.name.lower()
                for jk in catalog.join_keys
            )
            to_one.setdefault(left.table, {})[right.table] = forward
            to_one.setdefault(right.table, {})[left.table] = not forward

    def is_grain(alias: str) -> bool:
        seen, stack = {alias}, [alias]
        while stack:
            here = stack.pop()
            for nxt, one in to_one.get(here, {}).items():
                if nxt in seen:
                    continue
                if not one:
                    return False
                seen.add(nxt)
                stack.append(nxt)
        return True

    errors: list[ValidationError] = []
    for agg in aggs:
        if isinstance(agg, _FAN_OUT_SAFE) or agg.find(exp.Distinct):
            continue
        aliases = {c.table for c in agg.find_all(exp.Column)}
        ok = all(is_grain(a) for a in aliases) if aliases else any(is_grain(a) for a in to_one)
        if not ok:
            errors.append(
                _error(
                    "sql.fan_out",
                    f"{agg.sql()} would be inflated: a one-to-many join repeats its rows. "
                    "Aggregate the many-side table, pre-aggregate in a subquery, or use "
                    "COUNT(DISTINCT ...) / MIN / MAX",
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
