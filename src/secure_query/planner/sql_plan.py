"""SQL planner path: the model writes SQL in the catalog's dialect; the kernel validates and regenerates it.

The production NL path (ADR 005). The model's text never runs: kernel.sql_validate parses it, resolves
every column against the catalog, enforces policy and emits the SQL that runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import sqlglot
from sqlglot import exp

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
    similar_stored,
    stored_elsewhere,
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
  A path through other tables counts (a -> b -> c). Two tables that point to the same
  column may be joined on their pointing columns.
- Never read columns marked [pii=high], not even in WHERE or ORDER BY. If the question
  needs one, refuse.
- Return exactly the columns the question asks for. Use DISTINCT when it asks for
  distinct or different values.
- For "which X has the most/least ...", return that one X (ORDER BY ... LIMIT 1).
- For "each"/"every"/"all" groups, return every group (no small LIMIT).
- Refuse only when the catalog truly lacks what the question needs: no listed table or
  column holds it, or no chain of listed relationships connects the tables. Then return
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


_NUMBER = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


def _model_width(sql: str, dialect: str) -> int | None:
    """How many columns the model's own SELECT has; the kernel only appends after them."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    if not isinstance(tree, exp.Select) or any(isinstance(e, exp.Star) for e in tree.expressions):
        return None
    return len(tree.expressions)


def _probe_fingerprint(probe: ValueProbe, compiled: Any, width: int | None = None) -> str | None:
    """Fingerprint of one probe run; None when there is nothing to run or it fails.

    The gate compares what each reading answered: its own first `width` columns (a ranking
    measure the kernel appended is evidence, not the answer), with a number stored as text
    equal to the number. Two readings that name the same winner by different measures agree.
    """
    if compiled is None:
        return None
    try:
        rows = probe(compiled)
    except Exception:  # noqa: BLE001 — a query that cannot run is not a confirmed answer
        return None
    cell = lambda v: float(v) if isinstance(v, str) and _NUMBER.match(v) else v  # noqa: E731
    return answer_fingerprint([tuple(cell(v) for v in row[:width]) for row in rows])


def _no_stored_value(table: str, column: str, value: str, similar: Sequence[str]) -> str:
    """What the person asking is told; `similar` are stored values they are allowed to read."""
    close = f" Stored values close to it: {', '.join(repr(s) for s in similar)}." if similar else ""
    return f"No stored {table}.{column} equals {value!r}, so I won't answer with a guess.{close}"


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
    answer_probe: ValueProbe | None = None,
) -> PlannerResult:
    """Ask for SQL, validate it, allow `max_repairs` repairs with the validator's errors.

    With `value_probe`, string literals are grounded against stored values first, and a
    literal no stored value equals costs the repair round: the model is told the value and
    which other column stores it, never a stored value. If one is still left, the answer
    would be empty or partial because of a guess, so the question is refused
    (`no_stored_value`) and the person asking is shown close stored values.

    With `check_client` (a sampling client) and `value_probe`, the question is planned a
    second time independently and both queries are run: different answers mean the model
    is guessing, so it refuses instead (self-consistency; measured on BIRD it beats the
    keyword guards on both answer rate and precision). The second reading runs beside the
    first, so the gate costs one wait, not two.

    `answer_probe` runs the two readings for that comparison. It should return as many rows
    as the answer itself may hold; `value_probe` (small, for value lookups) is the fallback,
    and two answers that only differ after its last row would then pass as equal.
    """
    reading = partial(
        _plan_one_reading,
        question,
        catalog,
        max_repairs=max_repairs,
        row_filters=row_filters,
        prompt_catalog=prompt_catalog,
        relevant_tables=relevant_tables,
        value_probe=value_probe,
        answer_probe=answer_probe or value_probe,
    )
    if check_client is None or value_probe is None:
        return reading(client, answer_probe=None)
    pool = ThreadPoolExecutor(max_workers=1)
    second = pool.submit(reading, check_client)
    try:
        return reading(client, second_reading=second.result)
    finally:
        # The first reading may end without asking for the second (a refusal, an error):
        # cancel it if it has not started, otherwise let it finish unobserved.
        second.cancel()
        pool.shutdown(wait=False)


def _plan_one_reading(
    question: str,
    catalog: Catalog,
    client: LLMClient,
    *,
    max_repairs: int,
    row_filters: Sequence[Filter],
    prompt_catalog: Catalog | None,
    relevant_tables: Sequence[str],
    value_probe: ValueProbe | None,
    answer_probe: ValueProbe | None,
    second_reading: Callable[[], PlannerResult] | None = None,
) -> PlannerResult:
    """One reading of the question. `second_reading` returns the independent one to compare with."""
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
            width = _model_width(sql, catalog.sql_dialect)
            sql = show_ranking_measure(case_insensitive_like(sql, catalog), catalog)
            if value_probe is not None:
                sql = ground_literals(sql, catalog, value_probe, row_filters=row_filters)
            validated = validate_sql(sql, catalog, row_filters=row_filters)
            missing = (
                unmatched_literals(sql, catalog, value_probe, row_filters=row_filters)
                if value_probe is not None
                else []
            )
            if missing and (value_checked or attempt > max_repairs):
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempt,
                    errors=errors,
                    clarify_message=" ".join(
                        _no_stored_value(*m, similar_stored(*m, catalog, value_probe, row_filters=row_filters))
                        for m in missing
                    ),
                    raw_responses=raw_responses,
                    refused=True,
                    clarify_code="no_stored_value",
                    blocked=validated.compiled,
                )
            if missing:
                value_checked = True
                raise _NoStoredValue(
                    [
                        f"value: no stored {m[0]}.{m[1]} equals {m[2]!r}."
                        + "".join(
                            f" It is stored in {m[0]}.{other}."
                            for other in stored_elsewhere(*m, catalog, value_probe, row_filters=row_filters)
                        )
                        + " Check the column, the spelling and how values are coded."
                        for m in missing
                    ]
                )
            own_fp: str | None = None
            if answer_probe is not None:
                # Each reading runs once: its fingerprint is both the comparison and the evidence.
                # A query the database rejects is refused as invalid, not repaired: on BIRD dev
                # both readings repaired the same error the same way and agreed on wrong
                # answers (26 right, 63 wrong of 89 released), so the repair defeats the gate.
                own_fp = _probe_fingerprint(answer_probe, validated.compiled, width)
                if own_fp is None:
                    return PlannerResult(
                        status="clarify",
                        question=question,
                        attempts=attempt,
                        errors=[*errors, "execution: the database rejected the query"],
                        clarify_message="Could not produce a valid query. Please rephrase or narrow the question.",
                        raw_responses=raw_responses,
                        clarify_code="validation_failed",
                    )
            if second_reading is not None and own_fp is not None:
                second = second_reading()
                # The same query is the same answer: a capped, unordered result may come back
                # as a different sample of rows, which is not a disagreement.
                same_query = second.compiled is not None and second.compiled.sql == validated.compiled.sql
                second_fp = (second.gate_fingerprints or (_probe_fingerprint(answer_probe, second.compiled),))[0]
                if not same_query and second_fp != own_fp:
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
            return PlannerResult(
                status="ok",
                question=question,
                compiled=validated.compiled,
                source_sql=sql,
                attempts=attempt,
                raw_responses=raw_responses,
                gate_fingerprints=(own_fp,) if own_fp is not None else (),
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
