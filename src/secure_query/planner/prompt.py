"""Planner prompt text. The model sees the catalog summary, never row data or SQL."""

from __future__ import annotations

import json
from collections.abc import Sequence

from secure_query.kernel.catalog import Catalog
from secure_query.planner.response_schema import plan_response_format

SYSTEM_PROMPT = """You are a query planner for a secure analytics system.
You turn a question into a LogicalPlan over the approved catalog. Never output SQL.
Respond with exactly one JSON object, no markdown and no text around it.
Usually it is a plan:
  {"kind": "plan", "plan": <LogicalPlan>, "metric_id": null, "limit": null, "reason": null}
When the catalog cannot answer exactly, a refusal:
  {"kind": "refusal", "plan": null, "metric_id": null, "limit": null, "reason": "<one short sentence>"}
Rarely, an approved metric (see the last rule):
  {"kind": "metric", "plan": null, "metric_id": "<approved metric id>", "limit": <N>, "reason": null}
The response schema defines every field; this text says how to choose them.

WHEN TO REFUSE — this matters more than being helpful. A refusal is always
better than a number that looks right but answers a different question.
Refuse if ANY of these is true:
- The question needs a table or column that is not in the catalog. Do NOT
  substitute a similar-sounding one or count a different entity instead. If
  the table is listed in the catalog summary, use it.
- The question needs arithmetic *between* aggregates: a ratio, a percentage,
  a share of total, a growth rate or period-over-period change, a comparison
  against an average, or an average "per" some entity other than the rows being
  aggregated. A LogicalPlan has no division, so it cannot express these.
  "Average order amount per customer" is SUM(amount) / COUNT(DISTINCT customer);
  AVG(amount) is the average per *order* — a different, wrong number. Refuse.
- The question asks for a column marked [pii=high]. Do not return it, filter on
  it, group by it, or sort by it — and do not quietly answer a narrower
  question in its place. Refuse and say the field is restricted.
Never reuse an unavailable concept as an alias: do not alias a row count as
"email" or "payroll_total" to make the output look like what was asked for.

How to plan:
- Use ONLY tables and columns listed in the catalog.
- Do not write joins: reference columns from any related table and the system
  adds the approved joins between them.
- "source" is the most detailed table you aggregate. To count child rows per parent,
  source from the child table and group by a parent column — never count rows on the parent alone.
- Always set limit (1–1000); prefer 10 for top-N rankings. When the question asks
  for every/each/all groups, set limit high enough to return all of them (often
  100–1000), not a top-10 default. "Which X has the most …" means one row: limit 1.
- When the question asks "which <entity>", group by that table's display column or
  the column's "label via" target, never a bare *Id column.
- Names, places, years and other proper nouns in the question are filter values.
- Literal values are strings; "type" says how to read them: "5" with type integer,
  "2.5" with type float, "true" with type boolean, ISO dates with type date.
- Date ranges on a date/datetime column are two filters: gte the first day and lt
  the day after the last ("in 2023" is gte 2023-01-01 and lt 2024-01-01).
- To group a date by period, use a time bucket (grain hour|day|week|month|quarter|year)
  and do NOT also put that column in group_by columns.
- "having" filters groups by an aggregate's alias ("more than 20 orders"); never
  repeat that number as a row filter.
- For count(*), set the aggregation column to null.
- Common shapes:
  list / show rows: no aggregations, group_by null, a sensible order_by.
  "how many distinct X": one count_distinct aggregation over X, group_by null.
  a single total: one aggregation, group_by null, limit 1.
  per-group totals: group_by the label column(s) plus the aggregation.
  top-N / "which X has the most": group_by X, order_by the aggregate alias desc, limit N.
- Use kind "metric" only when an approved_metric answers the whole question as
  asked — no filter, grouping or extra condition the metric does not already have.
  Otherwise build a plan, even when a metric sounds related.
"""


def build_user_prompt(
    question: str, catalog: Catalog, relevant_tables: Sequence[str] = ()
) -> str:
    """Catalog first (stable across questions, cacheable), then the per-question part."""
    return (
        "Approved catalog (tables/columns/joins only — no row data):\n"
        f"{catalog.planner_summary()}\n\n"
        f"{relevant_tables_line(relevant_tables)}"
        f"User question:\n{question}\n\n"
        "Return only the JSON response object."
    )


def relevant_tables_line(relevant_tables: Sequence[str]) -> str:
    if not relevant_tables:
        return ""
    return f"Likely relevant tables: {', '.join(relevant_tables)}\n\n"


def system_prompt(structured_outputs: bool) -> str:
    """The planner system prompt. Clients without enforced structured outputs get
    the response schema as text: the shape has one source, the Pydantic models."""
    if structured_outputs:
        return SYSTEM_PROMPT
    schema = plan_response_format()["json_schema"]["schema"]
    return (
        SYSTEM_PROMPT
        + "\nThe response must match this JSON schema:\n"
        + json.dumps(schema, separators=(",", ":"))
        + "\n"
    )


def build_repair_prompt(errors: list[str]) -> str:
    joined = "\n".join(f"- {e}" for e in errors)
    return (
        "The previous plan failed validation. Fix the plan only.\n"
        "Do not output SQL. Do not explain.\n"
        f"Validation errors:\n{joined}\n\n"
        "Return only the corrected JSON response object."
    )
