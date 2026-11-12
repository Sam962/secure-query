"""SQL planner path: the model writes SQL in the catalog's dialect; the kernel validates and regenerates it.

The production NL path (ADR 005). The model's text never runs: kernel.sql_validate parses it, resolves
every column against the catalog, enforces policy and emits the SQL that runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import Filter
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.kernel.trace import answer_fingerprint
from secure_query.planner.llm import LLMClient
from secure_query.planner.plan import PlannerResult
from secure_query.planner.prompt import relevant_tables_line
from secure_query.planner.sql_values import (
    ValueProbe,
    case_insensitive_like,
    ground_literals,
    show_ranking_measure,
    unmatched_literals,
)

SQL_SYSTEM_PROMPT = """You translate a question into ONE read-only {dialect} SQL query over an approved catalog.
Output a single JSON object and nothing else:
  {"sql": "<one SELECT statement>"}
or, when the catalog cannot answer the question exactly:
  {"cannot_answer": true, "reason": "<one short sentence>"}

Rules:
- Use only the tables and columns listed in the catalog. {quoting}
- Join only along the listed relationships, with explicit JOIN ... ON a.key = b.key.
- Never read columns marked [pii=high], not even in WHERE or ORDER BY. If the question
  needs one, refuse.
- Return exactly the columns the question asks for. Use DISTINCT when it asks for
  distinct or different values.
- For "which X has the most/least ...", return that one X (ORDER BY ... LIMIT 1).
- For "each"/"every"/"all" groups, return every group (no small LIMIT).
- Refuse only when the catalog truly lacks what the question needs: no listed table or
  column holds it, or no listed relationship connects the tables. Then return
  cannot_answer instead of a nearby query.
- Do not refuse over wording or types. A column whose name is a synonym or abbreviation
  of the question's word is that column. A number or date stored as text can be CAST.
  Any column not marked [pii=high] may be read.
- Never write INSERT, UPDATE, DELETE, DDL, PRAGMA, ATTACH, COPY or table functions.
"""


_DIALECT_NAMES = {"duckdb": "DuckDB", "postgres": "PostgreSQL", "databricks": "Databricks"}
# In Databricks/Spark SQL "x" is a string literal, so identifiers take backticks.
_QUOTING = {"databricks": 'Quote identifiers with backticks (`Name`); "..." is a string.'}


def sql_system_prompt(dialect: str) -> str:
    return SQL_SYSTEM_PROMPT.replace("{dialect}", _DIALECT_NAMES.get(dialect, dialect)).replace(
        "{quoting}", _QUOTING.get(dialect, "Double-quote identifiers.")
    )


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


_UNSTABLE_MESSAGE = (
    "Two independent readings of this question returned different answers, so I won't "
    "guess. Name the exact measure, filters or time period and ask again."
)


def _probe_fingerprint(probe: ValueProbe, compiled: Any) -> str | None:
    """Fingerprint of one probe run; None when there is nothing to run or it fails."""
    if compiled is None:
        return None
    try:
        return answer_fingerprint(probe(compiled))
    except Exception:  # noqa: BLE001 — a query that cannot run is not a confirmed answer
        return None


class _NoStoredValue(Exception):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def plan_sql_question(
    question: str,
    catalog: Catalog,
    client: LLMClient,
    *,
    max_repairs: int = 1,
    row_filters: Sequence[Filter] = (),
    prompt_catalog: Catalog | None = None,
    relevant_tables: Sequence[str] = (),
    value_probe: ValueProbe | None = None,
    check_client: LLMClient | None = None,
) -> PlannerResult:
    """Ask for SQL, validate it, allow `max_repairs` repairs with the validator's errors.

    With `value_probe`, string literals are grounded against stored values first, and a
    literal no stored value equals costs the repair round (the value is named, stored
    values are not). If the model keeps it, the empty answer stands.

    With `check_client` (a sampling client) and `value_probe`, the question is planned a
    second time independently and both queries are run: different answers mean the model
    is guessing, so it refuses instead (self-consistency; measured on BIRD it beats the
    keyword guards on both answer rate and precision).
    """
    messages = [
        {"role": "system", "content": sql_system_prompt(catalog.sql_dialect)},
        {"role": "user", "content": build_sql_prompt(question, prompt_catalog or catalog, relevant_tables)},
    ]
    errors: list[str] = []
    raw_responses: list[str] = []
    value_checked = False
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
            validate_sql(sql, catalog, row_filters=row_filters)  # policy errors before any lookup
            sql = show_ranking_measure(case_insensitive_like(sql, catalog), catalog)
            if value_probe is not None:
                sql = ground_literals(sql, catalog, value_probe, row_filters=row_filters)
            validated = validate_sql(sql, catalog, row_filters=row_filters)
            missing = (
                unmatched_literals(sql, catalog, value_probe, row_filters=row_filters)
                if value_probe is not None and not value_checked and attempt <= max_repairs
                else []
            )
            if missing:
                value_checked = True
                raise _NoStoredValue(
                    [
                        f"value: no stored {table}.{column} equals {value!r}. Check the column, "
                        "the spelling and how values are coded; keep it only if it is meant to be absent."
                        for table, column, value in missing
                    ]
                )
            gate_fps: list[str] = []
            if check_client is not None and value_probe is not None:
                second = plan_sql_question(
                    question,
                    catalog,
                    check_client,
                    max_repairs=max_repairs,
                    row_filters=row_filters,
                    prompt_catalog=prompt_catalog,
                    relevant_tables=relevant_tables,
                    value_probe=value_probe,
                )
                # Each reading runs once: its fingerprint is both the comparison and the evidence.
                first_fp = _probe_fingerprint(value_probe, validated.compiled)
                second_fp = _probe_fingerprint(value_probe, second.compiled)
                if first_fp is None or first_fp != second_fp:
                    return PlannerResult(
                        status="clarify",
                        question=question,
                        attempts=attempt,
                        errors=errors,
                        clarify_message=_UNSTABLE_MESSAGE,
                        raw_responses=raw_responses,
                        refused=True,
                        clarify_code="unstable_answer",
                        blocked=validated.compiled,
                    )
                gate_fps.append(first_fp)
            return PlannerResult(
                status="ok",
                question=question,
                compiled=validated.compiled,
                source_sql=sql,
                attempts=attempt,
                raw_responses=raw_responses,
                gate_fingerprints=tuple(gate_fps),
            )
        except SqlValidationFailed as exc:
            batch = [f"{e.code}: {e.message}" for e in exc.errors]
        except _NoStoredValue as exc:
            batch = exc.messages
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
