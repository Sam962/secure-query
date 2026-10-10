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
from secure_query.kernel.compile import CompiledQuery, compile_filter, exists_through
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.joins import approved_path, grain_ok
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
# Functions sqlglot does not model as typed nodes, per dialect (only those that exist
# there). Any other unknown function, or any dialect not listed, is rejected.
_SAFE_ANONYMOUS = {
    "duckdb": frozenset({"strftime", "strptime", "date_part", "datediff", "date_diff"}),
    "postgres": frozenset({"date_part"}),
    "databricks": frozenset(),
}


# Functions that build values or rows from a number: one call can allocate gigabytes
# (repeat('x', 2e9), generate_series(1, 2e9)), and a statement timeout does not bound memory.
_UNBOUNDED_FUNCTIONS = tuple(
    getattr(exp, name)
    for name in ("Repeat", "Pad", "GenerateSeries", "ExplodingGenerateSeries", "GenerateDateArray")
    if hasattr(exp, name)
)


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
    dialect = catalog.sql_dialect  # the model is prompted for this dialect, too
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:  # ParseError, TokenError, …
        raise SqlValidationFailed([_error("sql.parse", str(exc).splitlines()[0])]) from exc
    if len(statements) != 1:
        raise SqlValidationFailed([_error("sql.statements", "exactly one statement is allowed")])
    tree = statements[0]
    if not isinstance(tree, exp.Query) or any(isinstance(n, _STATEMENTS) for n in tree.walk()):
        raise SqlValidationFailed([_error("sql.read_only", "only a read-only SELECT is allowed")])
    if any(w.args.get("recursive") for w in tree.find_all(exp.With)):
        raise SqlValidationFailed([_error("sql.recursive", "recursive CTEs are not allowed (unbounded)")])
    unbounded = sorted({type(f).__name__ for f in tree.find_all(*_UNBOUNDED_FUNCTIONS)})
    if unbounded:
        raise SqlValidationFailed(
            [_error("sql.function", f"{', '.join(unbounded)} can allocate unbounded memory; not allowed")]
        )
    misquoted = _misquoted_identifiers(tree, catalog, dialect)
    if misquoted:
        raise SqlValidationFailed(
            [
                _error(
                    "sql.quoted_identifier",
                    f"{', '.join(misquoted)} is a string literal in {dialect} SQL, not a column; "
                    "quote identifiers with backticks",
                )
            ]
        )

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
        if str(func.this).lower() not in _SAFE_ANONYMOUS.get(dialect, frozenset()):
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


def _misquoted_identifiers(tree: exp.Expression, catalog: Catalog, dialect: str) -> list[str]:
    """String literals that spell a catalog column, in dialects where "x" is a string.

    In Databricks/Spark SQL `SELECT "Name" FROM Genre WHERE "Name" = 'Rock'` is valid and
    returns the text 'Name' on every row with a filter that is never true: a silent wrong
    answer. Such a literal is almost always an identifier quoted the wrong way.
    """
    if '"' not in sqlglot.Dialect.get_or_raise(dialect).tokenizer_class.QUOTES:
        return []
    columns = {c.name.lower() for t in catalog.tables for c in t.columns}
    return sorted(
        {f'"{lit.this}"' for lit in tree.find_all(exp.Literal) if lit.is_string and lit.this.lower() in columns}
    )


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


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
            inner = inner.where(exists_through(path, filters, outer))
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
        conjuncts = _conjuncts(on)
        if any(c.find(exp.Or, exp.Not) for c in conjuncts):
            # `ON a.k = b.k OR TRUE` contains an approved equality but joins every row.
            errors.append(_error("sql.join", "JOIN ... ON must be equalities combined with AND (no OR / NOT)"))
            continue
        approved = False
        for eq in (c for c in conjuncts if isinstance(c, exp.EQ)):
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
    and MAX are unaffected.

    A CTE / derived side is resolved to its grain (_derived_grain): a plain
    single-table SELECT keeps its base table's grain and join keys; a grouped or
    single-row subquery is unique on its group keys. Anything else has unknown
    grain and the aggregate is refused (unless the catalog allows any join).
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or not select.args.get("joins"):
        return []
    aggs = [a for a in select.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select]
    if not aggs:
        return []
    tables = {t.name.lower(): t for t in catalog.tables}
    derived = {
        alias: _derived_grain(src, tables)
        for alias, src in scope.sources.items()
        if alias not in base
    }

    def resolve(col: exp.Column) -> tuple[str, str] | str | None:
        """(base table, column) for base or pass-through sides; "unique" when the
        column is a unique key of a grouped derived side; None when unknown."""
        if col.table in base:
            return base[col.table].name, col.name
        grain = derived.get(col.table)
        if grain is None:
            return None
        kind, info = grain
        if kind == "table":
            table, columns = info
            column = columns.get(col.name.lower())
            return (table, column) if column else None
        return "unique" if info is None or col.name.lower() in info else None

    to_one: dict[str, dict[str, bool]] = {}
    for join in select.args["joins"]:
        on = join.args.get("on")
        for eq in (c for c in _conjuncts(on) if isinstance(c, exp.EQ)) if on is not None else []:
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            ls, rs = resolve(left), resolve(right)
            if ls is None or rs is None or (ls == "unique" and rs == "unique"):
                if catalog.allow_any_join:
                    return []
                return [
                    _error(
                        "sql.fan_out_unknown_grain",
                        f"cannot tell how many rows {eq.sql()} repeats (derived table of "
                        "unknown grain); aggregate the base tables or pre-aggregate on the join key",
                    )
                ]
            if rs == "unique":
                # Every left row meets at most one derived row; the reverse holds too
                # when the left column is itself a key (the one side of a join key).
                forward = True
                to_one.setdefault(right.table, {})[left.table] = _is_key(catalog, ls)
                to_one.setdefault(left.table, {})[right.table] = True
                continue
            elif ls == "unique":
                to_one.setdefault(left.table, {})[right.table] = _is_key(catalog, rs)
                to_one.setdefault(right.table, {})[left.table] = True
                continue
            else:
                (lt, lc), (rt, rc) = ls, rs
                forward = any(
                    jk.left_table.lower() == lt.lower()
                    and jk.left_column.lower() == lc.lower()
                    and jk.right_table.lower() == rt.lower()
                    and jk.right_column.lower() == rc.lower()
                    for jk in catalog.join_keys
                )
            to_one.setdefault(left.table, {})[right.table] = forward
            to_one.setdefault(right.table, {})[left.table] = not forward

    errors: list[ValidationError] = []
    for agg in aggs:
        if isinstance(agg, _FAN_OUT_SAFE) or agg.find(exp.Distinct):
            continue
        aliases = {c.table for c in agg.find_all(exp.Column)}
        ok = (
            all(grain_ok(to_one, a) for a in aliases)
            if aliases
            else any(grain_ok(to_one, a) for a in to_one)
        )
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


def _is_key(catalog: Catalog, side: tuple[str, str] | str) -> bool:
    """A base column is unique when it is the one side of an approved join key."""
    if not isinstance(side, tuple):
        return False
    table, column = side
    return any(
        jk.right_table.lower() == table.lower() and jk.right_column.lower() == column.lower()
        for jk in catalog.join_keys
    )


def _derived_grain(source, tables: dict) -> tuple[str, object] | None:
    """Grain of a CTE / derived source, or None when it cannot be told.

    ("table", (base table, {output column: base column})) for a plain
    single-table SELECT; ("unique", group-key output columns, or None for a
    single-row aggregate) for a grouped subquery.
    """
    select = getattr(source, "expression", None)
    if not isinstance(select, exp.Select) or select.args.get("joins"):
        return None
    if select.args.get("group"):
        keys = {e.sql().lower() for e in select.args["group"].expressions}
        return "unique", {
            p.alias_or_name.lower()
            for p in select.expressions
            if (p.this if isinstance(p, exp.Alias) else p).sql().lower() in keys
        }
    if select.find(exp.AggFunc):
        return "unique", None
    if select.args.get("distinct"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    table = from_.this if from_ is not None else None
    if not isinstance(table, exp.Table) or table.name.lower() not in tables:
        return None
    columns = {}
    for p in select.expressions:
        inner = p.this if isinstance(p, exp.Alias) else p
        if isinstance(inner, exp.Column):
            columns[p.alias_or_name.lower()] = inner.name
    return "table", (tables[table.name.lower()].name, columns)


def _join_allowed(catalog: Catalog, lt: str, lc: str, rt: str, rc: str) -> bool:
    key = {(lt.lower(), lc.lower()), (rt.lower(), rc.lower())}
    return any(
        {(jk.left_table.lower(), jk.left_column.lower()), (jk.right_table.lower(), jk.right_column.lower())}
        == key
        for jk in catalog.join_keys
    )


def _limit_value(query: exp.Expression) -> int | None:
    limit = query.args.get("limit")
    if limit is None:
        return None
    node = limit.expression if isinstance(limit, exp.Limit) else limit
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.this)
    return None


# Outer-query clauses under which LIMIT n of the source equals LIMIT n of the result.
_PROJECTION_ONLY = frozenset({"expressions", "from", "from_", "limit", "with", "with_"})


def _projected_source(query: exp.Query) -> exp.Select | None:
    """The SELECT a bare-projection query reads, if it reads one CTE or subquery.

    Only then can the outer LIMIT be pushed in: any ORDER BY, OFFSET, WHERE, join,
    GROUP BY, DISTINCT, window or expression needs rows beyond the first n.
    """
    if not isinstance(query, exp.Select):
        return None
    if any(v for k, v in query.args.items() if k not in _PROJECTION_ONLY):
        return None
    for col in query.expressions:
        inner = col.this if isinstance(col, exp.Alias) else col
        if not isinstance(inner, (exp.Column, exp.Star)):
            return None
    from_ = query.args.get("from_") or query.args.get("from")
    src = from_.this if from_ is not None else None
    if isinstance(src, exp.Subquery):
        target = src.this
    elif isinstance(src, exp.Table) and not src.db:
        ctes = {c.alias_or_name.lower(): c.this for c in query.ctes}
        target = ctes.get(src.name.lower())
    else:
        return None
    return target if isinstance(target, exp.Select) else None


def _enforce_limit(tree: exp.Query, catalog: Catalog) -> None:
    """Cap the outer LIMIT, and push it into the CTE/subquery a bare projection reads.

    `WITH i AS (SELECT * FROM Invoice) SELECT * FROM i LIMIT 10` would otherwise
    materialize every Invoice row on a warehouse that does not inline the CTE.
    """
    value = _limit_value(tree)
    if value is None and not catalog.require_limit:
        return
    cap = min(value or catalog.max_limit, catalog.max_limit)
    if value is None or value > catalog.max_limit:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
    source = _projected_source(tree)
    if source is not None:
        inner = _limit_value(source)
        if inner is None or inner > cap:
            source.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
