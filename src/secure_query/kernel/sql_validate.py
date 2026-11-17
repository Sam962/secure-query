"""Validate model-written SQL against the approved catalog (the SQL planner path).

The model's SQL is never executed as written. It is parsed into an AST,
every column is resolved against the catalog schema (sqlglot `qualify`), and
the resolved tree must satisfy an **allowlist** policy:

  - exactly one read-only query (SELECT / UNION / INTERSECT / EXCEPT, CTEs allowed)
  - sources are catalog tables or CTEs/derived tables; no table functions
  - every projected/filtered/ordered expression resolves to an approved catalog
    column (or a pure function of those); no COLUMNS(), #n, STRUCT(*), whole-row
  - every join is an equi-join on an approved join key between base grains
  - only known SQL functions; no session/meta helpers (CURRENT_USER, VERSION, …)
  - aggregates over one-to-many joins (including laundered through a CTE) refuse
  - an outer LIMIT no larger than the catalog maximum (no LIMIT n PERCENT)

The SQL that runs is regenerated from the validated tree, never the model text.

Principal row filters are injected by replacing each filtered base table with a
filtered subquery, so no part of the query can see unfiltered rows.
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
# Nodes that read columns without becoming Column refs — allowlist rejects them.
_FORBIDDEN_SHAPES = tuple(
    getattr(exp, name)
    for name in (
        "Columns",  # DuckDB COLUMNS('Email') / COLUMNS(*)
        "PositionalColumn",  # #12
        "Struct",  # STRUCT(*) / struct(c.*) packs every column including PII
        "TableSample",
        "Version",  # VERSION AS OF — time-travel / alternate snapshot
        "HistoricalData",  # DuckDB AT (VERSION => n): reads rows deleted since, e.g. for privacy
        # Who and where the query runs (CURRENT_DATE and the clock are allowed: they read no
        # data and "how old" / "this year" need them; the answer then depends on the day).
        "CurrentUser",
        "CurrentSchema",
        "CurrentCatalog",
        "CurrentDatabase",
        "CurrentVersion",
        "SessionParameter",
        "Lock",  # FOR UPDATE / FOR SHARE — not a read-only snapshot
        "TableColumn",  # Postgres whole-row `SELECT c FROM t c`
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
    limit: int | None = None
    """Outer LIMIT value after enforcement (None = no LIMIT in original SQL)."""


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
    shape_errors = _forbid_shapes(tree)
    if shape_errors:
        raise SqlValidationFailed(shape_errors)
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
        if table.args.get("version") is not None or table.args.get("sample") is not None:
            raise SqlValidationFailed(
                [_error("sql.forbidden", "VERSION AS OF / TABLESAMPLE are not allowed")]
            )

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
    # Qualify rewrites Postgres `SELECT c FROM t c` into TableColumn; catch it here.
    table_column = getattr(exp, "TableColumn", None)
    if table_column is not None and any(isinstance(n, table_column) for n in tree.walk()):
        errors.append(
            _error(
                "sql.whole_row",
                "whole-row table references are not allowed; name the columns",
            )
        )
    # scope.columns omits `c.*` under CAST/COUNT; walk the tree for starred columns.
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            errors.append(
                _error(
                    "sql.star",
                    "qualified star / whole-row references are not allowed; name the columns",
                )
            )
            break
    # Unqualified column whose name is a source alias = whole-row reference.
    source_names = {
        a.lower()
        for scope in traverse_scope(tree)
        for a in scope.sources
    }
    for column in tree.find_all(exp.Column):
        if not column.table and column.name.lower() in source_names:
            errors.append(
                _error(
                    "sql.whole_row",
                    f"{column.name} names a table alias, not a column; name the columns",
                )
            )
            break

    for scope in traverse_scope(tree):
        base = {
            alias: tables[src.name.lower()]
            for alias, src in scope.sources.items()
            if isinstance(src, exp.Table) and src.name.lower() in tables
        }
        derived_aliases = {
            alias for alias, src in scope.sources.items() if alias not in base
        }
        source_aliases = {a.lower() for a in scope.sources}
        for column in scope.columns:
            if isinstance(column.this, exp.Star):
                continue  # already reported above
            # Whole-row leftovers: alias names a source, not a column.
            if not column.table and column.name.lower() in source_aliases:
                errors.append(
                    _error(
                        "sql.whole_row",
                        f"{column.name} names a table alias, not a column; name the columns",
                    )
                )
                continue
            spec_table = base.get(column.table)
            if (
                spec_table is None
                and column.table
                and column.table not in derived_aliases
                and column.table.lower() not in source_aliases
            ):
                # Correlated reference: the column belongs to a source of an enclosing
                # query. It gets the same catalog and PII checks as a local column.
                outer = scope.parent
                while outer is not None and column.table not in outer.sources:
                    outer = outer.parent
                if outer is None:
                    errors.append(_error("sql.unknown_column", column.sql()))
                    continue
                source = outer.sources[column.table]
                if isinstance(source, exp.Table) and source.name.lower() in tables:
                    spec_table = tables[source.name.lower()]
            if spec_table is None:
                continue  # CTE / derived output: validated in its own scope
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
        errors.extend(_check_aggregate_over_joined_derived(scope, base, catalog))
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
    original_limit = _limit_value(tree)
    if original_limit is not None:
        _break_ties(tree)
    _enforce_limit(tree, catalog)
    # Comments are inert in one statement, but model-written text then never reaches the
    # warehouse's query history or the evidence record.
    out = tree.sql(dialect=catalog.sql_dialect, comments=False)
    digest = hashlib.sha256(out.encode()).hexdigest()
    return ValidatedSql(
        compiled=CompiledQuery(sql=out, plan_hash=f"sql:{digest[:16]}", sql_hash=digest, parameters=[]),
        columns=frozenset(used),
        tables=read_tables,
        filter_strings=strings,
        filter_numbers=numbers,
        aggregates=aggregates,
        limit=original_limit,
    )


def _forbid_shapes(tree: exp.Expression) -> list[ValidationError]:
    """Reject AST shapes that read columns without becoming allowlisted Column refs."""
    errors: list[ValidationError] = []
    if _FORBIDDEN_SHAPES:
        found = sorted({type(n).__name__ for n in tree.find_all(*_FORBIDDEN_SHAPES)})
        if found:
            errors.append(
                _error(
                    "sql.forbidden",
                    f"{', '.join(found)} is not allowed; name approved catalog columns only",
                )
            )
    for limit in tree.find_all(exp.Limit):
        # LIMIT 100 PERCENT / LIMIT (SELECT …) bypass the integer row cap.
        node = limit.expression if isinstance(limit, exp.Limit) else limit
        options = limit.args.get("limit_options")
        percent = bool(getattr(options, "args", {}).get("percent")) if options is not None else False
        if percent or not (isinstance(node, exp.Literal) and node.is_int):
            errors.append(
                _error(
                    "sql.limit",
                    "LIMIT must be a fixed integer (PERCENT / expressions are not allowed)",
                )
            )
            break
    return errors


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
    """Every JOIN ON must be AND-ed equalities on an approved key between *base* grains.

    A derived / CTE side does not auto-approve: `ON b.k = b.k` is a Cartesian product
    that used to slip through when either side was non-base.
    """
    select = scope.expression
    if not isinstance(select, exp.Select):
        return []
    tables = {t.name.lower(): t for t in catalog.tables}
    derived = {
        alias: _derived_grain(src, tables)
        for alias, src in scope.sources.items()
        if alias not in base
    }
    errors: list[ValidationError] = []
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is None:
            if join.args.get("using"):
                errors.append(_error("sql.join", "JOIN ... USING is not allowed; use ON a.key = b.key"))
            elif (grain := derived.get(join.this.alias_or_name)) and grain[0] == "unique" and not grain[1][1]:
                continue  # one row: pairing with it repeats nothing
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
            # Same-alias equality (b.k = b.k) never connects two relations.
            if left.table and right.table and left.table == right.table:
                continue
            ls, rs = _resolve_join_side(left, base, derived), _resolve_join_side(right, base, derived)
            if ls is None or rs is None:
                if catalog.allow_any_join:
                    approved = True
                    continue
                errors.append(
                    _error(
                        "sql.join_unknown_grain",
                        f"cannot verify join key {eq.sql()} against the catalog "
                        "(derived table of unknown grain); join base tables on an approved key",
                    )
                )
                continue
            (lt, lc), (rt, rc) = ls, rs
            if catalog.allow_any_join or _join_allowed(catalog, lt, lc, rt, rc):
                approved = True
            else:
                errors.append(
                    _error(
                        "sql.join_not_allowed",
                        f"{lt}.{lc} = {rt}.{rc} is not an approved join key",
                    )
                )
        if not approved and not any(e.code.startswith("sql.join") for e in errors):
            errors.append(_error("sql.join", "each JOIN needs an equality on an approved join key"))
    return errors


def _resolve_join_side(
    col: exp.Column, base: dict, derived: dict
) -> tuple[str, str] | None:
    """(base_table, column) for a join side, or None when the grain is unknown."""
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
    # Grouped single-table subquery: group-key outputs map back to base columns.
    base_name, mapping = info
    if base_name is None:
        return None
    column = mapping.get(col.name.lower())
    return (base_name, column) if column else None


def _as_set(select: exp.Select) -> bool:
    """The rows of `select` are only used as a set: under IN / EXISTS, or a branch of a
    set operation that removes duplicates. Repeated rows cannot change the answer there."""
    parent = select.parent
    while isinstance(parent, (exp.Subquery, exp.Paren)):
        parent = parent.parent
    if isinstance(parent, (exp.In, exp.Exists, exp.Intersect, exp.Except)):
        return True
    return isinstance(parent, exp.Union) and bool(parent.args.get("distinct"))


def _check_unused_joins(scope) -> list[ValidationError]:
    """A joined table none of whose columns are used repeats or drops rows silently.

    Allowed when the query deduplicates or aggregates (DISTINCT, GROUP BY, aggregates)
    or its rows are used as a set (`_as_set`), where the join can only act as an existence
    filter. A table whose columns another join's ON uses is a bridge on the join path,
    not unused (a -> bridge -> c).
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or not select.args.get("joins"):
        return []
    if select.args.get("distinct") or select.args.get("group") or select.find(exp.AggFunc) or _as_set(select):
        return []
    on_columns = {
        id(col) for join in select.args["joins"] for col in (join.args.get("on") or exp.null()).find_all(exp.Column)
    }
    used = {col.table for col in scope.columns if id(col) not in on_columns}
    for join in select.args["joins"]:  # bridges: named in the ON of a join other than their own
        own = join.this.alias_or_name
        used.update(c.table for c in (join.args.get("on") or exp.null()).find_all(exp.Column) if c.table != own)
    errors = []
    for join in select.args["joins"]:
        alias = join.this.alias_or_name
        if alias not in used:
            errors.append(
                _error(
                    "sql.unused_join",
                    f"{join.this.name} is joined but none of its columns are used, which "
                    f"repeats or drops rows. If it is a filter, keep the condition as a WHERE "
                    f"EXISTS subquery on {join.this.name}; remove the join only "
                    "if the question does not need it",
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
        base_name, mapping = info
        if not mapping:  # single-row aggregate: every join to it is to-one
            return "unique"
        if col.name.lower() in mapping:
            return "unique" if base_name is None else (base_name, mapping[col.name.lower()])
        return None

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
            sibling = not forward and _siblings(catalog, lt, lc, rt, rc)
            to_one.setdefault(left.table, {})[right.table] = forward
            to_one.setdefault(right.table, {})[left.table] = not forward and not sibling

    errors: list[ValidationError] = []
    for agg in aggs:
        if isinstance(agg, _FAN_OUT_SAFE) or agg.find(exp.Distinct):
            continue
        conditions = {id(c) for cond in agg.find_all(exp.If) for c in cond.this.find_all(exp.Column)}
        aliases = {c.table for c in agg.find_all(exp.Column) if id(c) not in conditions}
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
    single-table SELECT; ("unique", (base table or None, {output: base col}))
    for a grouped / single-row aggregate subquery, whether or not it joins tables:
    it is unique on its group keys, and its own aggregates are checked in its own
    scope. The base table is the one every group-key column comes from (None when
    they come from several). None when a non-aggregating join makes grain unknown
    (those CTEs must not be aggregated or joined as if approved).
    """
    select = getattr(source, "expression", None)
    if not isinstance(select, exp.Select):
        return None
    joined = bool(select.args.get("joins"))
    from_ = select.args.get("from_") or select.args.get("from")
    table = from_.this if from_ is not None else None
    aliases = {
        src.alias_or_name: tables[src.name.lower()].name
        for src in [table, *(j.this for j in select.args.get("joins") or [])]
        if isinstance(src, exp.Table) and src.name.lower() in tables
    }
    base_name = (
        tables[table.name.lower()].name
        if not joined and isinstance(table, exp.Table) and table.name.lower() in tables
        else None
    )
    if select.args.get("group"):
        keys = {e.sql().lower() for e in select.args["group"].expressions}
        mapping: dict[str, str] = {}
        key_tables = set()
        for p in select.expressions:
            inner = p.this if isinstance(p, exp.Alias) else p
            if isinstance(inner, exp.Column) and inner.sql().lower() in keys:
                mapping[p.alias_or_name.lower()] = inner.name
                key_tables.add(aliases.get(inner.table) if inner.table else (base_name or None))
            elif (p.this if isinstance(p, exp.Alias) else p).sql().lower() in keys:
                mapping[p.alias_or_name.lower()] = p.alias_or_name
                key_tables.add(None)
        if not mapping:
            return None  # many rows, none of its keys shown: nothing to be unique on
        if joined:
            base_name = key_tables.pop() if len(key_tables) == 1 else None
        return "unique", (base_name, mapping)
    if select.find(exp.AggFunc) and not any(
        a.find_ancestor(exp.Window) for a in select.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select
    ):
        return "unique", (base_name, {})
    if joined or select.args.get("distinct"):
        return None
    if base_name is None:
        return None
    columns = {}
    for p in select.expressions:
        inner = p.this if isinstance(p, exp.Alias) else p
        if isinstance(inner, exp.Column):
            columns[p.alias_or_name.lower()] = inner.name
    return "table", (base_name, columns)


def _check_aggregate_over_joined_derived(scope, base: dict, catalog: Catalog) -> list[ValidationError]:
    """Refuse aggregates over a CTE/subquery that itself joined tables (fan-out launder).

    `WITH j AS (SELECT i.Total AS t, … FROM Invoice i JOIN InvoiceLine …) SELECT SUM(t) FROM j`
    has no outer join, so the per-join fan-out check never runs — yet SUM(t) is inflated.
    """
    select = scope.expression
    if not isinstance(select, exp.Select):
        return []
    aggs = [
        a
        for a in select.find_all(exp.AggFunc)
        if a.find_ancestor(exp.Select) is select and not isinstance(a, _FAN_OUT_SAFE) and not a.find(exp.Distinct)
    ]
    if not aggs:
        return []
    tables = {t.name.lower(): t for t in catalog.tables}
    for alias, src in scope.sources.items():
        if alias in base:
            continue
        # Joined derived bodies have unknown grain (_derived_grain → None).
        if _derived_grain(src, tables) is not None:
            continue
        body = getattr(src, "expression", None)
        if not isinstance(body, exp.Select) or not body.args.get("joins"):
            continue
        return [
            _error(
                "sql.fan_out",
                f"aggregate over {alias} would be inflated: that subquery joins tables. "
                "Pre-aggregate inside the subquery, or aggregate the base tables directly",
            )
        ]
    return []


def _join_allowed(catalog: Catalog, lt: str, lc: str, rt: str, rc: str) -> bool:
    key = {(lt.lower(), lc.lower()), (rt.lower(), rc.lower())}
    return _siblings(catalog, lt, lc, rt, rc) or any(
        {(jk.left_table.lower(), jk.left_column.lower()), (jk.right_table.lower(), jk.right_column.lower())}
        == key
        for jk in catalog.join_keys
    )


def _siblings(catalog: Catalog, lt: str, lc: str, rt: str, rc: str) -> bool:
    """Both columns point at the same approved key (a.parent_id = b.parent_id): a join
    of two many sides through their shared parent, many-to-many in both directions."""

    def parents(table: str, column: str) -> set[tuple[str, str]]:
        return {
            (jk.right_table.lower(), jk.right_column.lower())
            for jk in catalog.join_keys
            if jk.left_table.lower() == table.lower() and jk.left_column.lower() == column.lower()
        }

    return (lt.lower(), lc.lower()) != (rt.lower(), rc.lower()) and bool(parents(lt, lc) & parents(rt, rc))


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


def _break_ties(tree: exp.Query) -> None:
    """Order by every output column last, so LIMIT keeps the same tied rows on every run."""
    order = tree.args.get("order")
    if not isinstance(tree, exp.Select) or order is None:
        return
    order.set(
        "expressions",
        [
            *order.expressions,
            *(exp.Ordered(this=exp.Literal.number(i)) for i in range(1, len(tree.expressions) + 1)),
        ],
    )
    tree.set("order", order)


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
