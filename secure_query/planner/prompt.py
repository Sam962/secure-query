"""Planner system prompt: domain-agnostic rules plus shape examples drawn from the catalog.

Nothing here may name a specific dataset. Domain knowledge (which table holds
line items, what "revenue" means) belongs in the catalog's instructions,
synonyms, and metrics, which reach the model through Catalog.planner_summary().
Shape examples are built from whatever catalog is loaded, so the model sees the
JSON forms on its own tables rather than on a demo schema it might copy.
"""

from __future__ import annotations

import json
from typing import Any

from secure_query.kernel.catalog import Catalog, ColumnSpec, TableSpec

SYSTEM_PROMPT = """You are a query planner for a secure analytics system.
You MUST output a single JSON object. Never output SQL.
Never wrap the JSON in markdown fences. Never include explanations outside JSON.

Output ONE of two objects:
1. A LogicalPlan, when the catalog can answer the question exactly.
2. A refusal, when it cannot:
   {"cannot_answer": true, "reason": "<one short sentence>"}

WHEN TO REFUSE — this matters more than being helpful. A refusal is always
better than a number that looks right but answers a different question.
Refuse if ANY of these is true:
- The question needs a table or column that is not in the catalog. Do NOT
  substitute a similar-sounding one or count a different entity instead.
  Check the catalog first: if the table is listed, use it.
- The question needs arithmetic *between* aggregates: a ratio, a rate, a
  percentage, a share of total, a growth rate or period-over-period change, or
  an average "per" some entity other than the rows being aggregated (that is a
  total divided by a distinct count, not AVG of a column). This IR has no
  division, so you cannot express it — unless an approved metric matches.
- The question needs a comparison against another aggregate (e.g. "more than
  the average customer"): that needs a subquery, which this IR cannot express.
- The question asks for a column marked [pii=high]. Do not return it, filter on
  it, group by it, or sort by it — and do not quietly answer a narrower
  question in its place. Refuse and say the field is restricted.
Never reuse an unavailable concept as an alias: do not alias a row count with
the name of a field you could not return.

Rules:
- Use ONLY tables and columns listed in the catalog, spelled exactly as listed.
- Use ONLY approved joins from the catalog when joining. Every column you
  reference (in filters, group_by, aggregations, order_by) must belong to the
  source table or to a table you joined.
- Follow the catalog's instructions: they define business terms for this domain.
- Always set limit (1–1000) unless the catalog forbids it; prefer 10 for top-N rankings.
  When the question asks for every/each/all categories, set limit high enough to
  return all groups (often 100–1000), not a top-10 default.
- To count child rows per parent (items per order, members per group), source
  from the child/detail table and join the parent for labels — never count rows
  on the parent alone.
- When the question asks "which <thing>", group by that table's display column or
  the FK's label (both shown in the catalog), never a bare *_id.
- "How many different/distinct X" is a single count_distinct of X's key with no
  group_by on that key.
- Place names, years, statuses and other literal values in the question are filter
  values, not unknown schema concepts.
- Column refs are objects: {"table_id": "...", "column_id": "..."}.
- Filter literals: {"type": "string"|"integer"|"float"|"boolean"|"date"|"datetime", "value": ...}.
  Dates are ISO strings ("2023-01-01").
- Filters are a discriminated union on "op": eq, ne, lt, lte, gt, gte, in, not_in,
  between, is_null, not_null, like.
  "in"/"not_in" take "values" (a list of literals); "between" takes "low" and "high".
  Do NOT use "left"/"right" in filters — those are only for join conditions.
  Do NOT use {"type": "list", ...} — that is invalid.
- Date ranges on a date/datetime column: two filters, gte the start and lt the day
  after the end. "in 2023" is gte "2023-01-01" and lt "2024-01-01".
- group_by is either null or {"columns": [...], "time_buckets": []} — never a bare list.
- Time buckets group a date/datetime column into periods: {"column": ColumnRef,
  "grain": "hour"|"day"|"week"|"month"|"quarter"|"year"}. There is no "unit" or
  "offset" key. Put a bucketed column only in time_buckets, not also in "columns".
- Aggregations: {"fn": "count"|"count_distinct"|"sum"|"avg"|"min"|"max",
  "column": ColumnRef|null, "alias": "..."}. For count(*), set "column": null.
- To keep only groups whose aggregate passes a test ("more than 20 orders"), use
  "having": [{"alias": "<an aggregation alias>", "op": "gt", "value": {...}}].
  having never references table columns; filters on raw rows go in "filters".
- order_by items take either {"alias": ...} or {"column": ColumnRef}, plus "direction".
  To rank by a measure, put its alias first.
- Do not invent tables, columns, or join keys. Do not include SQL anywhere.
- Include "schema_version": "lqp/1" and "plan_id" as any UUID string.
- Alternatively return {"metric_id": "<approved_metric>", "limit": N} when a catalog
  approved_metric matches the question exactly (prefer this whenever one does).
"""


def build_system_prompt(catalog: Catalog) -> str:
    """Rules plus JSON shape examples on this catalog's own tables."""
    examples = _shape_examples(catalog)
    if not examples:
        return SYSTEM_PROMPT
    body = "\n\n".join(f"{title}:\n{json.dumps(obj, indent=2)}" for title, obj in examples)
    return (
        f"{SYSTEM_PROMPT}\n"
        "Shape examples on this catalog (illustrative values; plan the actual question):\n\n"
        f"{body}\n"
    )


def _shape_examples(catalog: Catalog) -> list[tuple[str, Any]]:
    tables = catalog.table_map()
    out: list[tuple[str, Any]] = []

    label = _first_column(catalog.tables, lambda c: c.dtype == "str")
    dated = _first_column(catalog.tables, lambda c: c.dtype == "datetime")

    if label is not None:
        table, col = label
        out.append(
            (
                "Filter-and-list",
                _plan(
                    source=table.name,
                    filters=[_eq(table.name, col.name, "<value from the question>")],
                    order_by=[{"column": _ref(table.name, col.name), "direction": "asc"}],
                    limit=20,
                ),
            )
        )

    if dated is not None:
        table, col = dated
        out.append(
            (
                "Trend per period within a date range",
                _plan(
                    source=table.name,
                    filters=[
                        {"op": "gte", "column": _ref(table.name, col.name),
                         "value": {"type": "date", "value": "2023-01-01"}},
                        {"op": "lt", "column": _ref(table.name, col.name),
                         "value": {"type": "date", "value": "2024-01-01"}},
                    ],
                    group_by={"columns": [], "time_buckets": [
                        {"column": _ref(table.name, col.name), "grain": "month"}
                    ]},
                    aggregations=[{"fn": "count", "column": None, "alias": "row_count"}],
                    limit=100,
                ),
            )
        )

    join = _join_with_label(catalog, tables)
    if join is not None:
        child, parent, child_col, parent_col, label_col, measure = join
        agg = (
            {"fn": "sum", "column": _ref(child.name, measure.name), "alias": f"total_{measure.name}"}
            if measure is not None
            else {"fn": "count", "column": None, "alias": "row_count"}
        )
        out.append(
            (
                "Aggregate per parent, joined for labels, keeping groups above a threshold, ranked",
                _plan(
                    source=child.name,
                    joins=[{
                        "right_table": parent.name,
                        "kind": "inner",
                        "conditions": [{"left": _ref(child.name, child_col), "right": _ref(parent.name, parent_col)}],
                    }],
                    group_by={"columns": [_ref(parent.name, label_col)], "time_buckets": []},
                    aggregations=[agg],
                    having=[{"alias": agg["alias"], "op": "gt", "value": {"type": "integer", "value": 10}}],
                    order_by=[{"alias": agg["alias"], "direction": "desc"}],
                    limit=10,
                ),
            )
        )
    return out


def _plan(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "plan_id": "00000000-0000-0000-0000-000000000001",
        "schema_version": "lqp/1",
        "source": fields.pop("source"),
        "joins": [],
        "filters": [],
        "group_by": None,
        "aggregations": [],
        "having": [],
        "order_by": [],
    }
    base.update(fields)
    limit = base.pop("limit", None)
    base["limit"] = limit
    return base


def _ref(table: str, column: str) -> dict[str, str]:
    return {"table_id": table, "column_id": column}


def _eq(table: str, column: str, value: str) -> dict[str, Any]:
    return {"op": "eq", "column": _ref(table, column), "value": {"type": "string", "value": value}}


def _first_column(tables: list[TableSpec], want) -> tuple[TableSpec, ColumnSpec] | None:
    for table in tables:
        for col in table.columns:
            if col.pii_risk != "high" and want(col):
                return table, col
    return None


def _join_with_label(catalog: Catalog, tables: dict[str, TableSpec]):
    """An approved join whose parent side has a readable label column."""
    for jk in catalog.join_keys:
        for child_name, child_col, parent_name, parent_col in (
            (jk.left_table, jk.left_column, jk.right_table, jk.right_column),
            (jk.right_table, jk.right_column, jk.left_table, jk.left_column),
        ):
            child, parent = tables.get(child_name), tables.get(parent_name)
            if child is None or parent is None or not parent.display_column:
                continue
            label = parent.column_map().get(parent.display_column.split(".", 1)[-1])
            if label is None or label.pii_risk == "high":
                continue
            measure = next(
                (c for c in child.columns
                 if c.dtype == "float" and c.pii_risk != "high"),
                None,
            )
            return child, parent, child_col, parent_col, label.name, measure
    return None
