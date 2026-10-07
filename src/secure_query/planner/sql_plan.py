"""SQL planner path: the model writes SQL in the catalog's dialect; the kernel validates and regenerates it.

Experimental alternative to the LogicalPlan path (see docs/STATUS.md, Spider
results). The model's text never runs: kernel.sql_validate parses it, resolves
every column against the catalog, enforces policy and emits the SQL that runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import Filter
from secure_query.kernel.sql_validate import SqlValidationFailed, ValidatedSql, validate_sql
from secure_query.planner.clarify import ClarifyCode
from secure_query.planner.guard import (
    Concept,
    lists_instead_of_count,
    missing_average,
    missing_concepts,
    unmatched_values,
)
from secure_query.planner.llm import LLMClient
from secure_query.planner.plan import PlannerResult
from secure_query.planner.prompt import relevant_tables_line
from secure_query.planner.sql_values import ValueProbe, ground_literals

SQL_SYSTEM_PROMPT = """You translate a question into ONE read-only {dialect} SQL query over an approved catalog.
Output a single JSON object and nothing else:
  {"sql": "<one SELECT statement>"}
or, when the catalog cannot answer the question exactly:
  {"cannot_answer": true, "reason": "<one short sentence>"}

Rules:
- Use only the tables and columns listed in the catalog. Double-quote identifiers.
- Join only along the listed relationships, with explicit JOIN ... ON a.key = b.key.
- Never read columns marked [pii=high], not even in WHERE or ORDER BY. If the question
  needs one, refuse.
- Return exactly the columns the question asks for. Use DISTINCT when it asks for
  distinct or different values.
- For "which X has the most/least ...", return that one X (ORDER BY ... LIMIT 1).
- For "each"/"every"/"all" groups, return every group (no small LIMIT).
- Refuse rather than approximate: if the catalog lacks a needed table, column or value,
  return cannot_answer instead of a nearby query.
- Never write INSERT, UPDATE, DELETE, DDL, PRAGMA, ATTACH, COPY or table functions.
"""


_DIALECT_NAMES = {"duckdb": "DuckDB", "postgres": "PostgreSQL", "databricks": "Databricks"}


def sql_system_prompt(dialect: str) -> str:
    return SQL_SYSTEM_PROMPT.replace("{dialect}", _DIALECT_NAMES.get(dialect, dialect))


def build_sql_prompt(question: str, catalog: Catalog, relevant_tables: Sequence[str] = ()) -> str:
    return (
        "Approved catalog (tables/columns/joins only — no row data):\n"
        f"{catalog.planner_summary()}\n\n"
        f"{relevant_tables_line(relevant_tables)}"
        f"Question:\n{question}\n\n"
        'Return only the JSON object: {"sql": ...} or {"cannot_answer": true, ...}.'
    )


def _parse(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("output must be a JSON object")
    return data


def sql_refusal(
    question: str, validated: ValidatedSql, catalog: Catalog
) -> tuple[ClarifyCode, str] | None:
    """The LogicalPlan path's semantic guards, applied to what the validated SQL reads.

    Arithmetic refusals (percent, growth, ratios) do not apply: SQL can compute them.
    """
    used = {Concept(table=t) for t in validated.tables}
    used |= {Concept(table=t, column=c) for t, c in validated.columns}
    numbers = list(validated.filter_numbers)
    checks: tuple[tuple[ClarifyCode, str | None], ...] = (
        ("dropped_concept", missing_concepts(question, used, catalog)),
        ("dropped_concept", missing_average(question, averaged="avg" in validated.aggregates)),
        ("dropped_concept", lists_instead_of_count(question, aggregated=bool(validated.aggregates))),
        (
            "dropped_filter",
            unmatched_values(
                question,
                [*validated.filter_strings, *(f"{n:g}" for n in numbers)],
                list(validated.filter_strings),
                numbers,
                catalog,
            ),
        ),
    )
    return next(((code, msg) for code, msg in checks if msg is not None), None)


def plan_sql_question(
    question: str,
    catalog: Catalog,
    client: LLMClient,
    *,
    max_repairs: int = 1,
    row_filters: Sequence[Filter] = (),
    guard: bool = True,
    prompt_catalog: Catalog | None = None,
    relevant_tables: Sequence[str] = (),
    value_probe: ValueProbe | None = None,
) -> PlannerResult:
    """Ask for SQL, validate it, allow `max_repairs` repairs with the validator's errors.

    With `value_probe`, string literals are grounded against stored values first.
    """
    messages = [
        {"role": "system", "content": sql_system_prompt(catalog.sql_dialect)},
        {"role": "user", "content": build_sql_prompt(question, prompt_catalog or catalog, relevant_tables)},
    ]
    errors: list[str] = []
    raw_responses: list[str] = []
    for attempt in range(1, max_repairs + 2):
        raw = client.complete(messages)
        raw_responses.append(raw)
        try:
            data = _parse(raw)
            if data.get("cannot_answer"):
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempt,
                    errors=errors,
                    clarify_message=str(data.get("reason") or "The planner declined to answer."),
                    raw_responses=raw_responses,
                    refused=True,
                    refused_by_model=True,
                    clarify_code="planner_refusal",
                )
            sql = str(data["sql"])
            validated = validate_sql(sql, catalog, row_filters=row_filters)
            if value_probe is not None:
                grounded = ground_literals(sql, catalog, value_probe, row_filters=row_filters)
                if grounded != sql:
                    sql, validated = grounded, validate_sql(grounded, catalog, row_filters=row_filters)
            refusal = sql_refusal(question, validated, catalog) if guard else None
            if refusal is not None:
                code, message = refusal
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempt,
                    errors=errors,
                    clarify_message=message,
                    raw_responses=raw_responses,
                    refused=True,
                    clarify_code=code,
                    blocked=validated.compiled,
                )
            return PlannerResult(
                status="ok",
                question=question,
                compiled=validated.compiled,
                source_sql=sql,
                attempts=attempt,
                raw_responses=raw_responses,
            )
        except SqlValidationFailed as exc:
            batch = [f"{e.code}: {e.message}" for e in exc.errors]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            batch = [f"output: {exc}"]
        errors.extend(batch)
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": "The query was rejected:\n"
                + "\n".join(f"- {e}" for e in batch)
                + '\nReturn only the corrected JSON object ({"sql": ...} or cannot_answer).',
            }
        )
    return PlannerResult(
        status="clarify",
        question=question,
        attempts=max_repairs + 1,
        errors=errors,
        clarify_message="Could not produce a valid query. Please rephrase or narrow the question.",
        raw_responses=raw_responses,
        clarify_code="validation_failed",
    )
