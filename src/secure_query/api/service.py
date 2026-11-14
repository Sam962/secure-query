"""Single ask pipeline used by the HTTP API and CLI.

Identity is resolved by the caller. This module never reads principal fields
from the question payload.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
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
from secure_query.kernel.evidence import METRIC_CHECKS, PLAN_PATH_CHECKS, SQL_PATH_CHECKS, build_evidence
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.metrics import get_metric
from secure_query.kernel.signing import mac, mac_matches
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.kernel.trace import (
    TraceTimer,
    catalog_hash,
    make_trace,
    result_fingerprint,
    write_trace,
)
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
from secure_query.planner.confidence import confidence_threshold, link_tables, low_confidence_message
from secure_query.planner.llm import get_check_client
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
    trace_id: str | None = None
    evidence: dict[str, Any] | None = None


class ReviewMismatch(Exception):
    """The artifact sent to execute no longer compiles to the reviewed plan_hash."""


class ReviewForbidden(Exception):
    """The review was not issued by this server for this principal (bad or missing signature)."""


def review_ttl_seconds() -> int:
    """How long a signed review may be executed (SECURE_QUERY_REVIEW_TTL_SECONDS, default 15 minutes)."""
    return int(os.environ.get("SECURE_QUERY_REVIEW_TTL_SECONDS") or 900)


def _review_message(review: dict[str, Any], principal: Principal, expires_at: int) -> str:
    return json.dumps(
        [
            principal.principal_id,
            principal.tenant_id,
            expires_at,
            {k: v for k, v in review.items() if k != "signature"},
        ],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sign_review(review: dict[str, Any], principal: Principal, *, now: float | None = None) -> str:
    """`<expires_at>.<hmac>` binding a review to the principal it was issued to, for a limited time.

    /ask/execute recomputes plan_hash from the artifact it is sent, so the hash alone
    cannot tell a reviewed query from one the caller wrote; the signature can.
    """
    expires_at = int((time.time() if now is None else now) + review_ttl_seconds())
    return f"{expires_at}.{mac(_review_message(review, principal, expires_at))}"


def verify_review(review: dict[str, Any], principal: Principal, signature: str) -> None:
    """Raise ReviewForbidden unless this server signed `review` for `principal` and it has not expired."""
    expires, _, digest = (signature or "").partition(".")
    if not expires.isdigit() or not mac_matches(_review_message(review, principal, int(expires)), digest):
        raise ReviewForbidden("review was not issued by this server for this principal")
    if int(expires) < time.time():
        raise ReviewForbidden("review has expired; ask again and confirm the new review")


# Value lookups that ground the SQL planner's string literals: small and fast.
_PROBE_OPTIONS = ExecuteOptions(max_rows=50, timeout_seconds=5.0)
_SQL_EXPLANATION = "Model-written SQL, validated and rewritten against the approved catalog"


def _client_model_id(client: LLMClient | None) -> str | None:
    if client is None:
        return None
    model = getattr(client, "_model", None)
    if isinstance(model, str) and model:
        return model
    provider = getattr(client, "provider", None)
    return str(provider) if provider else None


def _clarify(
    question: str,
    message: str,
    *,
    principal: Principal,
    code: ClarifyCode,
    refused: bool = True,
    retrieved_tables: list[str] | None = None,
    catalog: Catalog | None = None,
    config: RuntimeConfig | None = None,
    model_id: str | None = None,
    timer: TraceTimer | None = None,
) -> AskOutcome:
    suggestions = (
        suggest_questions(question, catalog, code=code) if catalog is not None else []
    )
    who = audit_principal_fields(principal)
    cat_hash = catalog_hash(catalog) if catalog is not None else None
    record = make_trace(
        status="refuse",
        question=question,
        principal_id=who["principal_id"],
        tenant_id=who["tenant_id"],
        backend=config.backend if config is not None else None,
        clarify_code=code,
        model_id=model_id,
        catalog_hash_value=cat_hash,
        duration_ms=timer.ms() if timer is not None else 0.0,
        refused=refused,
    )
    write_trace(record)
    evidence = build_evidence(
        kind="refuse",
        trace_id=record.trace_id,
        question=question,
        principal_id=who["principal_id"],
        tenant_id=who["tenant_id"],
        catalog_hash=cat_hash,
        model_id=model_id,
        clarify_code=code,
    )
    return AskOutcome(
        status="clarify",
        question=question,
        clarify_message=message,
        clarify_code=code,
        refused=refused,
        audit={**who, "trace_id": record.trace_id},
        retrieved_tables=retrieved_tables or [],
        suggestions=suggestions,
        trace_id=record.trace_id,
        evidence=evidence,
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
    timer = TraceTimer()
    question = (question or "").strip()
    active_client = client
    model_id = _client_model_id(active_client)
    too_long = question_too_long(question)
    if too_long:
        return _clarify(
            question, too_long, principal=principal, code="invalid_request",
            config=config, model_id=model_id, timer=timer,
        )
    if looks_like_sql_statement(question):
        return _clarify(
            question,
            "Submit a natural-language question. SQL is compiled by the server, not accepted from callers.",
            principal=principal,
            code="sql_in_question",
            config=config,
            model_id=model_id,
            timer=timer,
        )

    try:
        scoped = catalog_for_principal(catalog, principal)
    except PermissionError as exc:
        return _clarify(
            question, str(exc), principal=principal, code="auth",
            config=config, model_id=model_id, timer=timer,
        )

    if not question:
        return _clarify(
            question,
            "Question is empty",
            principal=principal,
            code="invalid_request",
            catalog=scoped,
            config=config,
            model_id=model_id,
            timer=timer,
        )

    ambiguous = ambiguous_metrics(question, scoped)
    if ambiguous:
        return _clarify(
            question,
            ambiguous,
            principal=principal,
            code="ambiguous_metric",
            catalog=scoped,
            config=config,
            model_id=model_id,
            timer=timer,
        )

    k = retrieve_k_from_env(len(scoped.tables))
    retrieved = retrieve_tables(question, scoped, k=k)
    prompt_catalog, relevant = prompt_catalog_and_hint(scoped, retrieved)

    sql_mode = planner_mode() == "sql"
    if sql_mode and k > 0:
        link = link_tables(question, scoped, k=k)
        if link.confidence < confidence_threshold() and len(scoped.tables) > 8:
            return _clarify(
                question,
                low_confidence_message(link),
                principal=principal,
                code="low_link_confidence",
                refused=True,
                retrieved_tables=list(link.tables),
                catalog=scoped,
                config=config,
                model_id=model_id,
                timer=timer,
            )
    if active_client is None:
        active_client = get_client()
        model_id = _client_model_id(active_client)
    if sql_mode:
        planned = plan_sql_question(
            question,
            scoped,
            active_client,
            max_repairs=1,
            row_filters=principal.row_filters,
            prompt_catalog=prompt_catalog,
            relevant_tables=relevant,
            value_probe=lambda probe: execute_compiled_query(
                probe, config, principal=principal, options=_PROBE_OPTIONS
            ).rows,
            # The gate compares the answers themselves, so it reads as many rows as an answer may hold.
            answer_probe=lambda reading: execute_compiled_query(
                reading, config, principal=principal, options=options
            ).rows,
            check_client=None if client else get_check_client(),
        )
    else:
        planned = plan_question(
            question,
            scoped,
            active_client,
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
            config=config,
            model_id=model_id,
            timer=timer,
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
                config=config,
                model_id=model_id,
                timer=timer,
            )
    elif compiled is None:
        return _clarify(
            question,
            "No plan or compiled SQL produced",
            principal=principal,
            code="validation_failed",
            retrieved_tables=retrieved,
            catalog=scoped,
            config=config,
            model_id=model_id,
            timer=timer,
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
                config=config,
                model_id=model_id,
                timer=timer,
            )

    assert compiled is not None

    if sql_mode:
        explanation, policy_checks = _SQL_EXPLANATION, SQL_PATH_CHECKS
    elif metric is not None and metric.kind != "plan":
        explanation, policy_checks = f"Approved metric {metric.id}: {metric.description}", METRIC_CHECKS
    elif plan is not None:
        explanation, policy_checks = explain_plan(plan), PLAN_PATH_CHECKS
    else:
        explanation, policy_checks = "Approved metric", METRIC_CHECKS
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
        policy_checks=policy_checks,
        model_id=model_id,
        timer=timer,
        gate_fingerprints=getattr(planned, "gate_fingerprints", ()) or (),
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
    verify_review(review, principal, signature)
    review["signature"] = signature
    timer = TraceTimer()
    try:
        scoped = catalog_for_principal(catalog, principal)
    except PermissionError as exc:
        return _clarify(
            question, str(exc), principal=principal, code="auth",
            config=config, timer=timer,
        )
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
        # Same branch order as _recompile: SQL, then metric, then LogicalPlan.
        policy_checks=(
            SQL_PATH_CHECKS if sql is not None else METRIC_CHECKS if metric_id is not None else PLAN_PATH_CHECKS
        ),
        timer=timer,
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
    policy_checks: tuple[str, ...],
    model_id: str | None = None,
    timer: TraceTimer | None = None,
    gate_fingerprints: tuple[str, ...] = (),
) -> AskOutcome:
    """Return the confirm view, or execute and format the answer."""
    cat_hash = catalog_hash(scoped)
    audit_base = {
        **audit_principal_fields(principal),
        "backend": config.backend,
        "plan_hash": compiled.plan_hash,
        "retrieved_tables": retrieved,
        "catalog_hash": cat_hash,
    }

    if confirm:
        record = make_trace(
            status="confirm",
            question=question,
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            backend=config.backend,
            model_id=model_id,
            catalog_hash_value=cat_hash,
            plan_hash=compiled.plan_hash,
            sql_hash=compiled.sql_hash,
            duration_ms=timer.ms() if timer is not None else 0.0,
        )
        write_trace(record)
        evidence = build_evidence(
            kind="confirm",
            trace_id=record.trace_id,
            question=question,
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            catalog_hash=cat_hash,
            model_id=model_id,
            plan_hash=compiled.plan_hash,
            sql_hash=compiled.sql_hash,
            policy_checks=policy_checks,
            gate_fingerprints=gate_fingerprints,
        )
        return AskOutcome(
            status="confirm",
            question=question,
            explanation=explanation,
            sql=compiled.sql,
            audit={**audit_base, "trace_id": record.trace_id},
            retrieved_tables=retrieved,
            plan=plan,
            compiled=compiled,
            review=review,
            trace_id=record.trace_id,
            evidence=evidence,
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
    fingerprint = result_fingerprint(result.rows, list(result.columns))
    record = make_trace(
        status="ok",
        question=question,
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        backend=config.backend,
        model_id=model_id,
        catalog_hash_value=cat_hash,
        plan_hash=compiled.plan_hash,
        sql_hash=compiled.sql_hash,
        result_fingerprint=fingerprint,
        duration_ms=timer.ms() if timer is not None else 0.0,
    )
    write_trace(record)
    evidence = build_evidence(
        kind="ok",
        trace_id=record.trace_id,
        question=question,
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        catalog_hash=cat_hash,
        model_id=model_id,
        plan_hash=compiled.plan_hash,
        sql_hash=compiled.sql_hash,
        result_fingerprint=fingerprint,
        policy_checks=policy_checks,
        gate_fingerprints=gate_fingerprints,
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
            "trace_id": record.trace_id,
            "result_fingerprint": fingerprint,
        },
        columns=list(result.columns),
        rows=rows,
        retrieved_tables=retrieved,
        column_units=column_units,
        scale_note=money_scale_note(result.columns, rows, column_units),
        plan=plan,
        compiled=compiled,
        review=review,
        trace_id=record.trace_id,
        evidence=evidence,
    )


def planner_mode() -> str:
    """SECURE_QUERY_PLANNER: "sql" (validated SQL, production) or "lqp" (LogicalPlan / metrics)."""
    mode = (os.environ.get("SECURE_QUERY_PLANNER") or "sql").strip().lower()
    return mode if mode in ("lqp", "sql") else "sql"


def production_auth_blocked() -> str | None:
    """Dev identity is forbidden when SECURE_QUERY_ENV=production."""
    env = (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower()
    mode = (os.environ.get("SECURE_QUERY_AUTH_MODE") or "").strip().lower()
    if env == "production" and mode == "dev":
        return "SECURE_QUERY_AUTH_MODE=dev is not allowed when SECURE_QUERY_ENV=production"
    return None
