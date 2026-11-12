"""Ground string literals in model-written SQL against stored values.

The model never sees row data, so it guesses how values are stored: 'Jetblue
Airways' for 'JetBlue Airways', 'North Carolina' for 'NorthCarolina', 'Europe'
for 'europe'. Each guess filters to an empty, confidently wrong answer.

For each `column = 'literal'` / `column IN (...)` on a string column, one bounded
lookup finds stored values that contain the literal's words. The lookup goes
through validate_sql, so principal row filters and PII rules apply to it. When
the literal is not stored and exactly one stored value equals it ignoring case,
spacing and punctuation, the literal is replaced. Stored values are never sent
to the model; any other outcome leaves the SQL as written.

`unmatched_literals` reports literals no stored value equals. On Spider dev and
test (about 860 answers) every such answer was a real error, so the planner
spends its repair round on them without revealing stored values.
`case_insensitive_like` makes LIKE patterns ignore case, as users mean "contains".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.logical_plan import Filter
from secure_query.kernel.sql_validate import validate_sql

ValueProbe = Callable[[CompiledQuery], Sequence[tuple[Any, ...]]]
"""Runs a compiled lookup query read-only and returns its rows."""

_MAX_LITERALS = 5
_MAX_CANDIDATES = 50


def ground_literals(
    sql: str, catalog: Catalog, probe: ValueProbe, *, row_filters: Sequence[Filter] = ()
) -> str:
    """`sql` with mis-spelled string literals replaced by the one stored value they name."""
    replacements: dict[str, str | None] = {}
    for table, column, value in _string_literals(sql, catalog)[:_MAX_LITERALS]:
        stored = _lookup(table, column, value, catalog, probe, row_filters)
        if stored is None or value in stored:
            continue
        same = {s for s in stored if _norm(s) == _norm(value)}
        target = same.pop() if len(same) == 1 else None
        # The same literal grounded two ways (two columns) is ambiguous: leave it.
        replacements[value] = target if replacements.get(value, target) == target else None
    replacements = {k: v for k, v in replacements.items() if v is not None}
    if not replacements:
        return sql
    tree = sqlglot.parse_one(sql, read=catalog.sql_dialect)
    for literal in list(tree.find_all(exp.Literal)):
        if literal.is_string and literal.this in replacements:
            literal.replace(exp.Literal.string(replacements[literal.this]))
    return tree.sql(dialect=catalog.sql_dialect)


def unmatched_literals(
    sql: str, catalog: Catalog, probe: ValueProbe, *, row_filters: Sequence[Filter] = ()
) -> list[tuple[str, str, str]]:
    """(table, column, literal) for string literals that no stored value equals."""
    return [
        (table, column, value)
        for table, column, value in _string_literals(sql, catalog)[:_MAX_LITERALS]
        if (stored := _lookup(table, column, value, catalog, probe, row_filters)) is not None
        and value not in stored
    ]


def case_insensitive_like(sql: str, catalog: Catalog) -> str:
    """Rewrite `x LIKE 'pattern'` as ILIKE: a user asking what contains a word means any case."""
    tree = sqlglot.parse_one(sql, read=catalog.sql_dialect)
    likes = [like for like in tree.find_all(exp.Like) if isinstance(like.expression, exp.Literal)]
    if not likes:
        return sql
    for like in likes:
        like.replace(exp.ILike(this=like.this.copy(), expression=like.expression.copy()))
    return tree.sql(dialect=catalog.sql_dialect)


def show_ranking_measure(sql: str, catalog: Catalog) -> str:
    """A ranked or thresholded answer shows the number it was ranked by.

    `SELECT name ... GROUP BY name ORDER BY COUNT(*) DESC LIMIT 1` names a winner but hides
    the count that made it win. Add each aggregate expression the outer ORDER BY sorts by,
    and each aggregate HAVING tests, that the SELECT list lacks. Grouped queries only, so
    the answer gains a column and never a row. It does not depend on the model or the
    prompt, and the result is validated like any other SQL.
    """
    tree = sqlglot.parse_one(sql, read=catalog.sql_dialect)
    if not isinstance(tree, exp.Select) or not tree.args.get("group") or tree.args.get("distinct"):
        return sql

    def own_aggregate(node: exp.Expression) -> bool:
        return node.find_ancestor(exp.Select) is tree and node.find_ancestor(exp.Window) is None

    order, having = tree.args.get("order"), tree.args.get("having")
    candidates = [o.this for o in (order.expressions if order is not None else ())]
    if having is not None:
        candidates += [a for a in having.find_all(exp.AggFunc) if a.find_ancestor(exp.AggFunc) is None]
    shown = {(p.this if isinstance(p, exp.Alias) else p).sql() for p in tree.expressions}
    names = {p.alias_or_name.lower() for p in tree.expressions}
    added = False
    for expr in candidates:
        if expr.sql() in shown or not any(own_aggregate(a) for a in expr.find_all(exp.AggFunc)):
            continue
        shown.add(expr.sql())
        columns = list(expr.find_all(exp.Column))
        base = expr.key if isinstance(expr, exp.AggFunc) else "ranked_by"
        name = f"{base}_{columns[0].name}".lower() if len(columns) == 1 else base
        while name in names:
            name += "_"
        names.add(name)
        tree.select(exp.alias_(expr.copy(), name, quoted=True), copy=False)
        added = True
    return tree.sql(dialect=catalog.sql_dialect) if added else sql


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value).lower())


def _string_literals(sql: str, catalog: Catalog) -> list[tuple[str, str, str]]:
    """(table, column, literal) for equality / IN predicates on catalog string columns."""
    qualified = sqlglot.parse_one(validate_sql(sql, catalog).compiled.sql, read=catalog.sql_dialect)
    found: list[tuple[str, str, str]] = []
    for scope in traverse_scope(qualified):
        tables = {
            alias: source.name
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table)
        }
        pairs: list[tuple[exp.Expression, exp.Expression]] = []
        for eq in scope.expression.find_all(exp.EQ):
            pairs += [(eq.this, eq.expression), (eq.expression, eq.this)]
        for in_ in scope.expression.find_all(exp.In):
            pairs += [(in_.this, v) for v in in_.expressions]
        for col, lit in pairs:
            if not (isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string):
                continue
            table = tables.get(col.table)
            spec = catalog.get_column(table, col.name) if table else None
            if spec is not None and spec.dtype == "str" and lit.this.strip():
                key = (table, spec.name, lit.this)
                if key not in found:
                    found.append(key)
    return found


def _lookup(
    table: str,
    column: str,
    value: str,
    catalog: Catalog,
    probe: ValueProbe,
    row_filters: Sequence[Filter],
) -> set[str] | None:
    """Stored values of table.column containing the literal's words, or None if unknown."""
    words = re.findall(r"[0-9a-z]+", value.lower())
    if not words:
        return None
    col = exp.column(column, table=table, quoted=True)
    query = (
        exp.select(col)
        .from_(exp.table_(table, quoted=True))
        .where(exp.Like(this=exp.Lower(this=col.copy()), expression=exp.Literal.string(f"%{'%'.join(words)}%")))
        .group_by(col.copy())  # not DISTINCT: Postgres requires ORDER BY items in a DISTINCT list
        .order_by(exp.Length(this=col.copy()))  # exact and near-exact values first
        .limit(_MAX_CANDIDATES)
    )
    try:
        compiled = validate_sql(query.sql(dialect=catalog.sql_dialect), catalog, row_filters=row_filters).compiled
        rows = probe(compiled)
    except Exception:  # noqa: BLE001 — a failed lookup leaves the literal as written
        return None
    return {r[0] for r in rows if isinstance(r[0], str)}
