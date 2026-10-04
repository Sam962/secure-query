"""Single ask pipeline used by the HTTP API and CLI.

Identity is resolved by the caller. This module never reads principal fields
from the question payload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from secure_query.auth import (
    Principal,
    assert_builtin_metric_allowed,
    audit_principal_fields,
    catalog_for_principal,
    inject_row_filters,
)
from secure_query.kernel.catalog import Catalog
from secure_query.planner.clarify import (
    ClarifyCode,
    ambiguous_metrics,
    code_from_guard_message,
    looks_like_sql_statement,
    question_too_long,
)
from secure_query.planner.suggest import SuggestedQuestion, suggest_questions
from secure_query.kernel.compile import CompiledQuery
from secure_query.engine.execute import ExecuteOptions, ExecutionError
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.planner import LLMClient, default_client, plan_question
from secure_query.api.respond import (
    format_result_value,
    format_template_answer,
    money_scale_note,
    result_column_units,
)
from secure_query.planner.retrieve import catalog_for_prompt, retrieve_k_from_env, retrieve_tables
from secure_query.engine.runtime import RuntimeConfig, execute_compiled_query
from secure_query.kernel.validate import (
    PlanValidationFailed,
    validate_and_compile,
    validate_and_compile_metric,
)


@dataclass
class AskOutcome:
    """Normalized result of one ask (confirm, execute, or clarify)."""

    status: Literal["ok", "confirm", "clarify"]
    question: str
    explanation: str | None = None
    sql: str | None = None
    answer: str | None = None
    clarify_message: str | None = None
    clarify_code: ClarifyCode | None = None
    refused: bool = False
    audit: dict[str, Any] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    retrieved_tables: list[str] = field(default_factory=list)
    suggestions: list[SuggestedQuestion] = field(default_factory=list)
    column_units: list[str | None] = field(default_factory=list)
    scale_note: str | None = None
    plan: LogicalPlan | None = None
    compiled: CompiledQuery | None = None


def _clarify(
    question: str,
    message: str,
    *,
    principal: Principal,
    code: ClarifyCode,
    refused: bool = True,
    retrieved_tables: list[str] | None = None,
    catalog: Catalog | None = None,
) -> AskOutcome:
    suggestions = (
        suggest_questions(question, catalog, code=code) if catalog is not None else []
    )
    return AskOutcome(
        status="clarify",
        question=question,
        clarify_message=message,
        clarify_code=code,
        refused=refused,
        audit=audit_principal_fields(principal),
        retrieved_tables=retrieved_tables or [],
        suggestions=suggestions,
    )


def ask(
    question: str,
    *,
    principal: Principal,
    config: RuntimeConfig,
    catalog: Catalog,
    client: LLMClient | None = None,
    confirm_only: bool = False,
    execute: bool = True,
    options: ExecuteOptions | None = None,
) -> AskOutcome:
    """Plan (and optionally execute) a question for an already-resolved principal."""
    question = (question or "").strip()
    too_long = question_too_long(question)
    if too_long:
        return _clarify(question, too_long, principal=principal, code="invalid_request")
    if looks_like_sql_statement(question):
        return _clarify(
            question,
            "Submit a natural-language question. SQL is compiled by the server, not accepted from callers.",
            principal=principal,
            code="sql_in_question",
        )

    try:
        scoped = catalog_for_principal(catalog, principal)
    except PermissionError as exc:
        return _clarify(question, str(exc), principal=principal, code="auth")

    if not question:
        return _clarify(
            question,
            "Question is empty",
            principal=principal,
            code="invalid_request",
            catalog=scoped,
        )

    ambiguous = ambiguous_metrics(question, scoped)
    if ambiguous:
        return _clarify(
            question,
            ambiguous,
            principal=principal,
            code="ambiguous_metric",
            catalog=scoped,
        )

    retrieved = retrieve_tables(question, scoped, k=retrieve_k_from_env(len(scoped.tables)))
    prompt_catalog = catalog_for_prompt(scoped, retrieved) if retrieved else scoped

    planned = plan_question(
        question,
        scoped,
        client or default_client(),
        max_repairs=1,
        prompt_catalog=prompt_catalog,
    )

    if planned.status != "ok" or (planned.compiled is None and planned.plan is None):
        code = code_from_guard_message(
            planned.clarify_message, refused=planned.refused
        ) or ("planner_refusal" if planned.refused else "validation_failed")
        return _clarify(
            question,
            planned.clarify_message or "Could not plan this question",
            principal=principal,
            code=code,
            refused=planned.refused,
            retrieved_tables=retrieved,
            catalog=scoped,
        )

    plan = planned.plan
    compiled = planned.compiled
    metric = planned.metric
    ratio = metric if metric is not None and metric.kind == "ratio" else None

    if plan is not None:
        plan = inject_row_filters(plan, principal)
        try:
            if ratio is not None:
                compiled = validate_and_compile_metric(ratio, plan, scoped)
            else:
                compiled = validate_and_compile(plan, scoped)
        except PlanValidationFailed as exc:
            return _clarify(
                question,
                str(exc),
                principal=principal,
                code="validation_failed",
                refused=False,
                retrieved_tables=retrieved,
                catalog=scoped,
            )
    elif compiled is None:
        return _clarify(
            question,
            "No plan or compiled SQL produced",
            principal=principal,
            code="validation_failed",
            retrieved_tables=retrieved,
            catalog=scoped,
        )
    else:
        try:
            assert_builtin_metric_allowed(principal)
        except PermissionError as exc:
            return _clarify(
                question,
                str(exc),
                principal=principal,
                code="auth",
                retrieved_tables=retrieved,
            )

    assert compiled is not None

    if metric is not None and metric.kind != "plan":
        explanation = f"Approved metric {metric.id}: {metric.description}"
    else:
        explanation = explain_plan(plan) if plan else "Approved metric"
    # A ratio's plan selects numerator/denominator, not the columns the answer shows.
    answer_plan = None if ratio is not None else plan
    audit_base = {
        **audit_principal_fields(principal),
        "backend": config.backend,
        "plan_hash": compiled.plan_hash,
        "retrieved_tables": retrieved,
    }

    if confirm_only or not execute:
        return AskOutcome(
            status="confirm",
            question=question,
            explanation=explanation,
            sql=compiled.sql,
            audit=audit_base,
            retrieved_tables=retrieved,
            plan=plan,
            compiled=compiled,
        )

    try:
        result = execute_compiled_query(
            compiled,
            config,
            plan=plan,
            question=question,
            principal=principal,
            options=options
            or ExecuteOptions(
                audit_path=config.audit_path,
            ),
        )
    except ExecutionError:
        raise

    answer = format_template_answer(
        question, answer_plan, result, catalog=scoped, compiled=compiled
    )
    rows = [[format_result_value(c) for c in row] for row in result.rows]
    column_units = result_column_units(
        list(result.columns), plan=answer_plan, catalog=scoped, compiled=compiled
    )
    return AskOutcome(
        status="ok",
        question=question,
        explanation=explanation,
        sql=compiled.sql,
        answer=answer,
        audit={
            **audit_base,
            "sql_hash": compiled.sql_hash,
            "row_count": len(result.rows),
        },
        columns=list(result.columns),
        rows=rows,
        retrieved_tables=retrieved,
        column_units=column_units,
        scale_note=money_scale_note(result.columns, rows, column_units),
        plan=plan,
        compiled=compiled,
    )


def production_auth_blocked() -> str | None:
    """Dev identity is forbidden when SECURE_QUERY_ENV=production."""
    env = (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower()
    mode = (os.environ.get("SECURE_QUERY_AUTH_MODE") or "").strip().lower()
    if env == "production" and mode == "dev":
        return "SECURE_QUERY_AUTH_MODE=dev is not allowed when SECURE_QUERY_ENV=production"
    return None
