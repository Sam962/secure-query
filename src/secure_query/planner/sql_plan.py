"""SQL planner path: the model writes DuckDB SQL; the kernel validates and regenerates it.

Experimental alternative to the LogicalPlan path (see docs/STATUS.md, Spider
results). The model's text never runs: kernel.sql_validate parses it, resolves
every column against the catalog, enforces policy and emits the SQL that runs.
"""

from __future__ import annotations

import json
import re

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.planner.llm import LLMClient
from secure_query.planner.plan import PlannerResult

SQL_SYSTEM_PROMPT = """You translate a question into ONE read-only DuckDB SQL query over an approved catalog.
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


def build_sql_prompt(question: str, catalog: Catalog) -> str:
    return (
        "Approved catalog (tables/columns/joins only — no row data):\n"
        f"{catalog.planner_summary()}\n\n"
        f"Question:\n{question}\n\n"
        'Return only the JSON object: {"sql": ...} or {"cannot_answer": true, ...}.'
    )


def _parse(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("output must be a JSON object")
    return data


def plan_sql_question(
    question: str, catalog: Catalog, client: LLMClient, *, max_repairs: int = 1
) -> PlannerResult:
    """Ask for SQL, validate it, allow `max_repairs` repairs with the validator's errors."""
    messages = [
        {"role": "system", "content": SQL_SYSTEM_PROMPT},
        {"role": "user", "content": build_sql_prompt(question, catalog)},
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
            compiled = validate_sql(str(data["sql"]), catalog)
            return PlannerResult(
                status="ok",
                question=question,
                compiled=compiled,
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
