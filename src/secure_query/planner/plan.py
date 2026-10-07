"""LLM planner: natural language → LogicalPlan JSON only (never SQL).

Flow:
    question + catalog.planner_summary()
      → LLM structured JSON
      → LogicalPlan (plan_id assigned locally)
      → validate_and_compile
      → on failure: one repair with error codes; then clarify

The model must not emit SQL. Repair prompts also must not include SQL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from uuid import uuid4

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.metrics import MetricSpec
from secure_query.kernel.validate import (
    PlanValidationFailed,
    normalize_plan,
    validate_and_compile,
    validate_and_compile_metric,
)
from secure_query.planner.clarify import ClarifyCode, code_from_guard_message
from secure_query.planner.guard import (
    averaged_per_other_entity,
    counted_other_entity,
    dropped_average,
    dropped_concepts,
    dropped_count,
    dropped_literals,
    inexpressible_request,
    opaque_grouping_keys,
    restricted_request,
    unrelated_metric,
)
from secure_query.planner.llm import LLMClient
from secure_query.planner.prompt import build_repair_prompt, build_user_prompt, system_prompt
from secure_query.planner.response_schema import plan_response_format, unwrap_response

_SQL_LEAK_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|WITH)\b",
    re.IGNORECASE,
)

_PostPlanGuard = Callable[[str, LogicalPlan, Catalog], str | None]

# Deterministic checks on a valid plan, in order, each with the code it reports.
# The first one that objects refuses the question.
_POST_PLAN_GUARDS: tuple[tuple[ClarifyCode, _PostPlanGuard], ...] = (
    ("analyst_handoff", lambda q, plan, catalog: inexpressible_request(q)),
    ("dropped_concept", opaque_grouping_keys),
    ("dropped_concept", dropped_concepts),
    ("dropped_concept", lambda q, plan, catalog: dropped_average(q, plan)),
    ("analyst_handoff", averaged_per_other_entity),
    ("dropped_concept", lambda q, plan, catalog: dropped_count(q, plan)),
    ("dropped_concept", counted_other_entity),
    ("dropped_filter", dropped_literals),
)


def _post_plan_refusal(
    question: str, plan: LogicalPlan, catalog: Catalog
) -> tuple[ClarifyCode, str] | None:
    for code, check in _POST_PLAN_GUARDS:
        message = check(question, plan, catalog)
        if message is not None:
            return code, message
    return None


@dataclass
class PlannerResult:
    """Outcome of plan_question."""

    status: Literal["ok", "clarify"]
    question: str
    plan: LogicalPlan | None = None
    compiled: CompiledQuery | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    clarify_message: str | None = None
    raw_responses: list[str] = field(default_factory=list)
    refused: bool = False
    """True when the model deliberately declined, vs. failing to produce a valid plan."""
    clarify_code: ClarifyCode | None = None
    metric: MetricSpec | None = None
    """Set for ratio/builtin metrics, which compile outside validate_and_compile."""
    refused_by_model: bool = False
    """The model declined. clarify_code is inferred from its wording (for the UI),
    so it must not be credited to a deterministic guard in eval scorecards."""
    source_sql: str | None = None
    """SQL path: the model's SQL that validated (re-validated by /ask/execute)."""
    blocked: CompiledQuery | None = None
    """A valid plan a post-plan guard refused. Never executed by the product; the
    eval harness runs it to score whether the guard blocked a right or wrong answer."""



class PlannerRefusal(Exception):
    """The model declined to answer, which is a valid terminal outcome.

    Distinct from a validation failure: there is nothing to repair, so the
    planner must not burn a retry trying to force a plan out of the model.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason



def _load_json(raw: str) -> Any:
    """Model output → JSON value: strip code fences, unwrap the structured-output wrapper."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(text)
    return unwrap_response(data) if isinstance(data, dict) else data


def parse_plan_json(raw: str, catalog: Catalog | None = None) -> LogicalPlan:
    """Parse model JSON into LogicalPlan; assign a fresh plan_id locally."""
    data = _load_json(raw)
    if not isinstance(data, dict):
        raise ValueError("Planner output must be a JSON object")
    if data.get("cannot_answer"):
        raise PlannerRefusal(str(data.get("reason") or "The planner declined to answer."))
    if "sql" in data or any(
        isinstance(v, str) and _SQL_LEAK_RE.search(v) for v in data.values() if isinstance(v, str)
    ):
        raise ValueError("Planner output must not contain SQL")
    if data.get("metric_id"):
        if catalog is None:
            raise ValueError("metric_id requires catalog to expand")
        from secure_query.kernel.metrics import (
            assert_metric_authorized,
            expand_metric_plan,
            metrics_for_catalog,
        )

        mid = str(data["metric_id"])
        metrics = {m.id: m for m in metrics_for_catalog(catalog)}
        if mid not in metrics:
            raise ValueError(f"Unknown metric_id: {mid!r}")
        metric = metrics[mid]
        try:
            assert_metric_authorized(metric, catalog)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        if metric.kind != "plan":
            raise ValueError(f"{metric.kind} metrics compile via try_compile_metric, not LogicalPlan")
        limit = data.get("limit")
        return expand_metric_plan(metric, limit=int(limit) if limit is not None else None)
    data["plan_id"] = str(uuid4())
    data.setdefault("schema_version", "lqp/1")
    # Joins come from the catalog (kernel.joins); whatever the model wrote is dropped.
    data.pop("joins", None)
    return LogicalPlan.model_validate(data)


def _metric_id(raw: str) -> str | None:
    """The metric_id a planner response asks for, if any (no validation)."""
    try:
        data = _load_json(raw)
    except json.JSONDecodeError:
        return None
    return str(data["metric_id"]) if isinstance(data, dict) and data.get("metric_id") else None


def try_compile_metric(
    raw: str, catalog: Catalog
) -> tuple[LogicalPlan | None, CompiledQuery | None, MetricSpec | None]:
    """Compile a ratio or builtin metric_id. Returns (plan, compiled, metric).

    Ratio metrics return their expanded plan so callers can inject row filters
    and recompile with validate_and_compile_metric. Builtin metrics have no plan.
    Anything else returns (None, None, None) and goes through parse_plan_json.
    """
    data = _load_json(raw)
    if not isinstance(data, dict) or not data.get("metric_id"):
        return None, None, None
    from secure_query.kernel.metrics import (
        assert_metric_authorized,
        compile_builtin_metric,
        expand_metric_plan,
        get_metric,
    )

    metric = get_metric(str(data["metric_id"]), catalog)
    if metric is None or metric.kind == "plan":
        return None, None, None
    try:
        assert_metric_authorized(metric, catalog)
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc

    if metric.kind == "ratio":
        limit = data.get("limit")
        plan = expand_metric_plan(metric, limit=int(limit) if limit is not None else None)
        return plan, validate_and_compile_metric(metric, plan, catalog), metric

    import hashlib

    assert metric.builder_id is not None
    sql = compile_builtin_metric(metric.builder_id, dialect=catalog.sql_dialect)
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()
    compiled = CompiledQuery(sql=sql, plan_hash=f"metric:{metric.id}", sql_hash=sql_hash, parameters=[])
    return None, compiled, metric


def plan_question(
    question: str,
    catalog: Catalog,
    client: LLMClient,
    *,
    max_repairs: int = 1,
    guard: bool = True,
    prompt_catalog: Catalog | None = None,
    relevant_tables: Sequence[str] = (),
) -> PlannerResult:
    """Ask the LLM for a LogicalPlan, validate/compile, optionally one repair.

    `guard` enables the deterministic question checks. Leave it on outside of
    ablation experiments: they are the only refusals that do not depend on the
    model choosing to cooperate.

    `prompt_catalog` may be a retrieval slice for the LLM prompt. Validation
    always uses `catalog` (the full authorized allowlist).
    """
    if max_repairs < 0:
        raise ValueError("max_repairs must be >= 0")

    summary_catalog = prompt_catalog or catalog

    if guard:
        blocked = restricted_request(question, catalog)
        if blocked is not None:
            return PlannerResult(
                status="clarify",
                question=question,
                attempts=0,
                clarify_message=blocked,
                refused=True,
                clarify_code="restricted_pii",
            )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt(getattr(client, "structured_outputs", False))},
        {"role": "user", "content": build_user_prompt(question, summary_catalog, relevant_tables)},
    ]
    errors: list[str] = []
    raw_responses: list[str] = []
    attempts = 0

    while True:
        attempts += 1
        raw = (
            client.complete(messages, response_format=plan_response_format())
            if getattr(client, "structured_outputs", False)
            else client.complete(messages)
        )
        raw_responses.append(raw)
        try:
            metric_id = _metric_id(raw)
            mismatch = unrelated_metric(question, metric_id) if guard and metric_id else None
            if mismatch is not None:
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempts,
                    errors=errors,
                    clarify_message=mismatch,
                    raw_responses=raw_responses,
                    refused=True,
                    clarify_code="dropped_concept",
                )
            metric_plan, metric_compiled, metric = try_compile_metric(raw, catalog)
            if metric_compiled is not None:
                return PlannerResult(
                    status="ok",
                    question=question,
                    plan=metric_plan,
                    compiled=metric_compiled,
                    metric=metric,
                    attempts=attempts,
                    errors=[],
                    raw_responses=raw_responses,
                )
            plan = normalize_plan(parse_plan_json(raw, catalog), catalog)
            compiled = validate_and_compile(plan, catalog)
            refusal = _post_plan_refusal(question, plan, catalog) if guard else None
            if refusal is not None:
                code, message = refusal
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempts,
                    errors=errors,
                    clarify_message=message,
                    raw_responses=raw_responses,
                    refused=True,
                    clarify_code=code,
                    blocked=compiled,
                )
            return PlannerResult(
                status="ok",
                question=question,
                plan=plan,
                compiled=compiled,
                attempts=attempts,
                errors=[],
                raw_responses=raw_responses,
            )
        except PlannerRefusal as refusal:
            return PlannerResult(
                status="clarify",
                question=question,
                attempts=attempts,
                errors=errors,
                clarify_message=refusal.reason,
                raw_responses=raw_responses,
                refused=True,
                clarify_code=code_from_guard_message(refusal.reason, refused=True)
                or "planner_refusal",
                refused_by_model=True,
            )
        except (json.JSONDecodeError, ValueError, PlanValidationFailed) as exc:
            if isinstance(exc, PlanValidationFailed):
                err_msgs = [f"{e.code}: {e.message}" for e in exc.errors]
            else:
                err_msgs = [str(exc)]
            errors.extend(err_msgs)
            if attempts > max_repairs:
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempts,
                    errors=errors,
                    clarify_message=(
                        "Could not produce a valid LogicalPlan after "
                        f"{attempts} attempt(s). Please rephrase or narrow the question."
                    ),
                    raw_responses=raw_responses,
                    clarify_code="validation_failed",
                )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": build_repair_prompt(err_msgs)})
