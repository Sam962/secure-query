"""Join paths come from the catalog, not the model.

The planner names the columns it reads; this module connects those tables
through catalog.join_keys. A wrong or missing join from the model can no
longer reach SQL, and the path used is the approved one, every time.

Resolution grows a tree from the source table: repeatedly take the nearest
unconnected table (fewest hops from any table already in the tree) and add its
shortest path. If two different shortest paths exist, the question is
ambiguous and the plan is rejected. Picking one silently could pick the wrong
relationship (e.g. billing vs. support-rep).

Auto-joining makes fan-out easy, so it is checked here too: walking from the
one side of a key to the many side repeats rows, which inflates SUM, AVG and
COUNT over the repeated table. Such plans are rejected, not silently wrong.
"""

from __future__ import annotations

from collections import deque

from secure_query.kernel.catalog import Catalog, JoinKey
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.logical_plan import ColumnRef, Join, JoinCondition, LogicalPlan

_Edge = tuple[str, JoinKey]  # (neighbour table, key)


def _graph(catalog: Catalog) -> dict[str, list[_Edge]]:
    graph: dict[str, list[_Edge]] = {}
    for jk in catalog.join_keys:
        if jk.left_table == jk.right_table:
            continue  # self-joins need two table instances; the IR has one
        graph.setdefault(jk.left_table, []).append((jk.right_table, jk))
        graph.setdefault(jk.right_table, []).append((jk.left_table, jk))
    return graph


def _referenced_tables(plan: LogicalPlan) -> set[str]:
    refs: list[ColumnRef | None] = []
    for filt in plan.filters:
        refs.append(filt.column)
        value = getattr(filt, "value", None)
        if isinstance(value, ColumnRef):
            refs.append(value)
    if plan.group_by is not None:
        refs.extend(plan.group_by.columns)
        refs.extend(b.column for b in plan.group_by.time_buckets)
    refs.extend(a.column for a in plan.aggregations)
    refs.extend(o.column for o in plan.order_by)
    tables = {r.table_id for r in refs if r is not None}
    # Tables the model joined but never reads still count: a joined lookup
    # table is how normalize_plan infers its display column as a group key.
    tables.update(j.right_table for j in plan.joins)
    return tables


def _error(code: str, message: str) -> ValidationError:
    return ValidationError(code=code, path="$.joins", message=message, stage="policy")


def resolve_joins(plan: LogicalPlan, catalog: Catalog) -> tuple[LogicalPlan, list[ValidationError]]:
    """Return the plan with joins rebuilt from catalog.join_keys, or errors.

    Catalogs without join_keys (dev `allow_any_join`) keep the model's joins.
    Unknown tables are left for validate() to report.
    """
    if not catalog.join_keys:
        return plan, []
    graph = _graph(catalog)
    known = set(catalog.table_map())
    pending = {t for t in _referenced_tables(plan) if t != plan.source and t in known}
    if plan.source not in known:
        return plan, []
    if not pending:
        return plan.model_copy(update={"joins": []}), []

    kinds = {j.right_table: j.kind for j in plan.joins if j.kind in ("inner", "left")}
    tree = {plan.source}
    joins: list[Join] = []

    while pending:
        # Multi-source BFS from the tree, counting shortest paths per table.
        dist = {t: 0 for t in tree}
        count = {t: 1 for t in tree}
        parent: dict[str, tuple[str, JoinKey]] = {}
        queue = deque(sorted(tree))
        while queue:
            node = queue.popleft()
            for nxt, key in graph.get(node, []):
                if nxt not in dist:
                    dist[nxt] = dist[node] + 1
                    count[nxt] = count[node]
                    parent[nxt] = (node, key)
                    queue.append(nxt)
                elif dist[nxt] == dist[node] + 1:
                    count[nxt] += count[node]

        unreachable = sorted(t for t in pending if t not in dist)
        if unreachable:
            return plan, [
                _error(
                    "plan.no_join_path",
                    f"No approved join path from {plan.source} to {', '.join(unreachable)}",
                )
            ]
        target = min(pending, key=lambda t: (dist[t], t))
        if count[target] > 1:
            return plan, [
                _error(
                    "plan.ambiguous_join_path",
                    f"More than one approved join path reaches {target}; refusing "
                    "rather than picking a relationship",
                )
            ]

        path: list[tuple[str, str, JoinKey]] = []  # (from, to, key)
        node = target
        while node not in tree:
            prev, key = parent[node]
            path.append((prev, node, key))
            node = prev
        for prev, node, key in reversed(path):
            left_col, right_col = (
                (key.left_column, key.right_column)
                if key.left_table == prev
                else (key.right_column, key.left_column)
            )
            joins.append(
                Join(
                    right_table=node,
                    kind=kinds.get(node, "inner"),
                    conditions=[
                        JoinCondition(
                            left=ColumnRef(table_id=prev, column_id=left_col),
                            right=ColumnRef(table_id=node, column_id=right_col),
                        )
                    ],
                )
            )
            tree.add(node)
            pending.discard(node)

    return plan.model_copy(update={"joins": joins}), []


def approved_path(catalog: Catalog, start: str, target: str) -> list[tuple[str, str, JoinKey]] | None:
    """The unique shortest join path start → target as (from, to, key) steps.

    None when target is unreachable. Raises ValueError when two shortest paths
    exist (same rule as resolve_joins: never pick a relationship silently).
    """
    graph = _graph(catalog)
    dist, count, parent = {start: 0}, {start: 1}, {}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt, key in graph.get(node, []):
            if nxt not in dist:
                dist[nxt], count[nxt], parent[nxt] = dist[node] + 1, count[node], (node, key)
                queue.append(nxt)
            elif dist[nxt] == dist[node] + 1:
                count[nxt] += count[node]
    if target not in dist:
        return None
    if count[target] > 1:
        raise ValueError(f"more than one approved join path from {start} to {target}")
    path = []
    node = target
    while node != start:
        prev, key = parent[node]
        path.append((prev, node, key))
        node = prev
    return list(reversed(path))


_FANOUT_SAFE_AGGS = {"min", "max", "count_distinct"}


def fan_out_errors(plan: LogicalPlan, catalog: Catalog) -> list[ValidationError]:
    """Reject aggregates whose rows the join tree repeats.

    `to_one[a][b]` is True when stepping from table a to b follows a key from
    its many side to its one side, which never repeats a's rows. An aggregate
    over table T is exact only if every step outward from T is to-one. COUNT(*)
    and list intents need some table to be that grain.
    """
    if not plan.joins:
        return []
    to_one: dict[str, dict[str, bool]] = {}
    for join in plan.joins:
        for cond in join.conditions:
            prev, node = cond.left, cond.right
            forward = any(
                jk.left_table == prev.table_id
                and jk.left_column == prev.column_id
                and jk.right_table == node.table_id
                and jk.right_column == node.column_id
                for jk in catalog.join_keys
            )
            to_one.setdefault(prev.table_id, {})[node.table_id] = forward
            to_one.setdefault(node.table_id, {})[prev.table_id] = not forward

    def is_grain(table: str) -> bool:
        seen, stack = {table}, [table]
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
    for i, agg in enumerate(plan.aggregations):
        if agg.fn in _FANOUT_SAFE_AGGS:
            continue
        if agg.column is None:
            ok = any(is_grain(t) for t in to_one)
            what = "COUNT(*)"
        else:
            ok = is_grain(agg.column.table_id)
            what = f"{agg.fn}({agg.column.table_id}.{agg.column.column_id})"
        if not ok:
            errors.append(
                ValidationError(
                    code="plan.fan_out",
                    path=f"$.aggregations[{i}]",
                    message=(
                        f"{what} would be inflated: the joins repeat its rows (one-to-many). "
                        "Aggregate the many-side table, or use count_distinct/min/max"
                    ),
                    stage="policy",
                )
            )
    if not plan.aggregations and plan.group_by is None and not any(is_grain(t) for t in to_one):
        errors.append(
            _error("plan.fan_out", "These joins repeat rows in more than one direction")
        )
    return errors
