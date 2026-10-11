"""Single ask pipeline used by the HTTP API and CLI.

Identity is resolved by the caller. This module never reads principal fields
from the question payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from secure_query.api.respond import (
    format_result_value,
    format_template_answer,
    money_scale_note,
    result_column_units,
)
from secure_query.auth import (
    Principal,
    assert_builtin_metric_allowed,
    audit_principal_fields,
    catalog_for_principal,
    inject_row_filters,
)
from secure_query.engine.execute import ExecuteOptions
from secure_query.engine.runtime import RuntimeConfig, execute_compiled_query
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.metrics import get_metric
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.kernel.validate import (
    PlanValidationFailed,
    validate_and_compile,
    validate_and_compile_metric,
)
from secure_query.planner import LLMClient, get_client, plan_question, try_compile_metric
from secure_query.planner.clarify import (
    ClarifyCode,
    ambiguous_metrics,
    looks_like_sql_statement,
    question_too_long,
)
from secure_query.planner.retrieve import prompt_catalog_and_hint, retrieve_k_from_env, retrieve_tables
from secure_query.planner.sql_plan import plan_sql_question
from secure_query.planner.suggest import SuggestedQuestion, suggest_questions


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
    review: dict[str, Any] | None = None
    """What /ask/execute needs to run exactly this query: plan_hash plus the
    pre-row-filter plan, the model's source SQL, or the metric id."""


class ReviewMismatch(Exception):
    """The artifact sent to execute no longer compiles to the reviewed plan_hash."""


class ReviewForbidden(Exception):
    """The review was not issued by this server for this principal (bad or missing signature)."""


@lru_cache(maxsize=1)
def _review_secret() -> bytes:
    """SECURE_QUERY_REVIEW_SECRET; without it a per-process key (one worker only)."""
    return (os.environ.get("SECURE_QUERY_REVIEW_SECRET") or "").encode() or secrets.token_bytes(32)


def sign_review(review: dict[str, Any], principal: Principal) -> str:
    """HMAC binding a review to the principal it was issued to.

    /ask/execute recomputes plan_hash from the artifact it is sent, so the hash alone
    cannot tell a reviewed query from one the caller wrote; the signature can.
    """
    payload = json.dumps(
        [principal.principal_id, principal.tenant_id, {k: v for k, v in review.items() if k != "signature"}],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hmac.new(_review_secret(), payload.encode(), hashlib.sha256).hexdigest()


# Value lookups that ground the SQL planner's string literals: small and fast.
_PROBE_OPTIONS = ExecuteOptions(max_rows=50, timeout_seconds=5.0)
_SQL_EXPLANATION = "Model-written SQL, validated and rewritten against the approved catalog"


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
    prompt_catalog, relevant = prompt_catalog_and_hint(scoped, retrieved)

    sql_mode = planner_mode() == "sql"
    if sql_mode:
        planned = plan_sql_question(
            question,
            scoped,
            client or get_client(),
            max_repairs=1,
            row_filters=principal.row_filters,
            prompt_catalog=prompt_catalog,
            relevant_tables=relevant,
            value_probe=lambda probe: execute_compiled_query(
                probe, config, principal=principal, options=_PROBE_OPTIONS
            ).rows,
        )
    else:
        planned = plan_question(
            question,
            scoped,
            client or get_client(),
            max_repairs=1,
            prompt_catalog=prompt_catalog,
            relevant_tables=relevant,
        )

    if planned.status != "ok" or (planned.compiled is None and planned.plan is None):
        code = planned.clarify_code or ("planner_refusal" if planned.refused else "validation_failed")
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
    reviewed_plan = plan  # before row filters: execute re-injects them server-side

    if sql_mode:
        pass  # validate_sql already applied the principal's row filters
    elif plan is not None:
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

    if sql_mode:
        explanation = _SQL_EXPLANATION
    elif metric is not None and metric.kind != "plan":
        explanation = f"Approved metric {metric.id}: {metric.description}"
    else:
        explanation = explain_plan(plan) if plan else "Approved metric"
    review = {
        "plan_hash": compiled.plan_hash,
        "plan": reviewed_plan.model_dump(mode="json") if reviewed_plan is not None and not sql_mode else None,
        "sql": planned.source_sql if sql_mode else None,
        "metric_id": metric.id if metric is not None and metric.kind != "plan" else None,
    }
    review["signature"] = sign_review(review, principal)
    return _finish(
        question,
        principal=principal,
        config=config,
        scoped=scoped,
        plan=plan,
        compiled=compiled,
        explanation=explanation,
        # A ratio's plan selects numerator/denominator, not the columns the answer shows.
        answer_plan=None if ratio is not None else plan,
        retrieved=retrieved,
        confirm=confirm_only or not execute,
        options=options,
        review=review,
    )


def execute_reviewed(
    question: str,
    *,
    principal: Principal,
    config: RuntimeConfig,
    catalog: Catalog,
    plan_hash: str,
    plan: dict[str, Any] | None = None,
    sql: str | None = None,
    metric_id: str | None = None,
    signature: str = "",
    options: ExecuteOptions | None = None,
) -> AskOutcome:
    """Run the artifact a user reviewed via confirm: no LLM call.

    The plan (or SQL, or metric) is re-validated against the principal's catalog
    and row filters, re-compiled, and must hash to `plan_hash`. Raises
    ReviewMismatch otherwise, so a re-plan can never run in place of the review.
    Raises ReviewForbidden unless `signature` is the one this server issued.
    """
    review = {"plan_hash": plan_hash, "plan": plan, "sql": sql, "metric_id": metric_id}
    if not hmac.compare_digest(sign_review(review, principal), signature or ""):
        raise ReviewForbidden("review was not issued by this server for this principal")
    review["signature"] = signature
    try:
        scoped = catalog_for_principal(catalog, principal)
    except PermissionError as exc:
        return _clarify(question, str(exc), principal=principal, code="auth")
    try:
        compiled, run_plan, explanation, answer_plan = _recompile(
            scoped, principal, plan=plan, sql=sql, metric_id=metric_id
        )
    except (PlanValidationFailed, SqlValidationFailed, PermissionError, ValueError) as exc:
        raise ReviewMismatch(f"reviewed plan no longer validates: {exc}") from exc
    if compiled.plan_hash != plan_hash:
        raise ReviewMismatch("reviewed plan no longer matches plan_hash")
    return _finish(
        question,
        principal=principal,
        config=config,
        scoped=scoped,
        plan=run_plan,
        compiled=compiled,
        explanation=explanation,
        answer_plan=answer_plan,
        retrieved=[],
        confirm=False,
        options=options,
        review=review,
    )


def _recompile(
    scoped: Catalog,
    principal: Principal,
    *,
    plan: dict[str, Any] | None,
    sql: str | None,
    metric_id: str | None,
) -> tuple[CompiledQuery, LogicalPlan | None, str, LogicalPlan | None]:
    """(compiled, executed plan, explanation, plan for answer formatting)."""
    if sql is not None:
        validated = validate_sql(sql, scoped, row_filters=principal.row_filters)
        return validated.compiled, None, _SQL_EXPLANATION, None
    if metric_id is not None:
        metric = get_metric(metric_id, scoped)
        if metric is None or metric.kind == "plan":
            raise ValueError(f"unknown metric {metric_id!r}")
        explanation = f"Approved metric {metric.id}: {metric.description}"
        if metric.kind == "builtin":
            assert_builtin_metric_allowed(principal)
            _, compiled, _ = try_compile_metric(json.dumps({"metric_id": metric_id}), scoped)
            assert compiled is not None
            return compiled, None, explanation, None
        if plan is None:
            raise ValueError("ratio metric review needs its plan")
        run_plan = inject_row_filters(LogicalPlan.model_validate(plan), principal)
        return validate_and_compile_metric(metric, run_plan, scoped), run_plan, explanation, None
    if plan is None:
        raise ValueError("execute needs plan, sql or metric_id")
    run_plan = inject_row_filters(LogicalPlan.model_validate(plan), principal)
    return validate_and_compile(run_plan, scoped), run_plan, explain_plan(run_plan), run_plan


def _finish(
    question: str,
    *,
    principal: Principal,
    config: RuntimeConfig,
    scoped: Catalog,
    plan: LogicalPlan | None,
    compiled: CompiledQuery,
    explanation: str,
    answer_plan: LogicalPlan | None,
    retrieved: list[str],
    confirm: bool,
    options: ExecuteOptions | None,
    review: dict[str, Any],
) -> AskOutcome:
    """Return the confirm view, or execute and format the answer."""
    audit_base = {
        **audit_principal_fields(principal),
        "backend": config.backend,
        "plan_hash": compiled.plan_hash,
        "retrieved_tables": retrieved,
    }

    if confirm:
        return AskOutcome(
            status="confirm",
            question=question,
            explanation=explanation,
            sql=compiled.sql,
            audit=audit_base,
            retrieved_tables=retrieved,
            plan=plan,
            compiled=compiled,
            review=review,
        )

    result = execute_compiled_query(
        compiled,
        config,
        plan=plan,
        question=question,
        principal=principal,
        options=options or ExecuteOptions(audit_path=config.audit_path),
    )

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
        review=review,
    )


def planner_mode() -> str:
    """SECURE_QUERY_PLANNER: "lqp" (default, LogicalPlan) or "sql" (validated SQL, see ADR 003)."""
    mode = (os.environ.get("SECURE_QUERY_PLANNER") or "lqp").strip().lower()
    return mode if mode in ("lqp", "sql") else "lqp"


def production_auth_blocked() -> str | None:
    """Dev identity is forbidden when SECURE_QUERY_ENV=production."""
    env = (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower()
    mode = (os.environ.get("SECURE_QUERY_AUTH_MODE") or "").strip().lower()
    if env == "production" and mode == "dev":
        return "SECURE_QUERY_AUTH_MODE=dev is not allowed when SECURE_QUERY_ENV=production"
    return None
