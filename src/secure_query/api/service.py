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
    audit_principal_fields,
    catalog_for_principal,
)
from secure_query.engine.execute import ExecuteOptions
from secure_query.engine.runtime import RuntimeConfig, execute_compiled_query
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.evidence import SQL_PATH_CHECKS, build_evidence
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.signing import mac, mac_matches
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.kernel.trace import (
    TraceTimer,
    catalog_hash,
    make_trace,
    result_fingerprint,
    write_trace,
)
from secure_query.planner import LLMClient, get_client
from secure_query.planner.clarify import (
    ClarifyCode,
    ambiguous_metrics,
    looks_like_sql_statement,
    question_too_long,
)
from secure_query.planner.confidence import confidence_threshold, link_tables, low_confidence_message
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
    review_only: bool = False,
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

    if k > 0:
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
        check_client=_second_reader(active_client),
    )

    if planned.status != "ok" or planned.compiled is None:
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

    compiled = planned.compiled  # validate_sql already applied the principal's row filters
    if planned.certified_id:
        approved = next((c for c in scoped.certified if c.id == planned.certified_id), None)
        explanation = f"Certified answer {planned.certified_id}: {approved.question if approved else ''}".rstrip(": ")
    else:
        explanation = _SQL_EXPLANATION
    policy_checks = SQL_PATH_CHECKS
    # plan and metric_id stay in the signed review (always null) so a client returns it unchanged.
    review = {"plan_hash": compiled.plan_hash, "plan": None, "sql": planned.source_sql, "metric_id": None}
    review["signature"] = sign_review(review, principal)
    return _finish(
        question,
        principal=principal,
        config=config,
        scoped=scoped,
        plan=None,
        compiled=compiled,
        explanation=explanation,
        answer_plan=None,
        retrieved=retrieved,
        confirm=review_only or not execute,
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
    """Run the query a user reviewed via /ask/review: no LLM call.

    The reviewed SQL is re-validated against the principal's catalog and row filters,
    re-compiled, and must hash to `plan_hash`. Raises
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
    if sql is None or plan is not None or metric_id is not None:
        raise ReviewMismatch("execute takes the reviewed SQL only")
    try:
        compiled = validate_sql(sql, scoped, row_filters=principal.row_filters).compiled
    except SqlValidationFailed as exc:
        raise ReviewMismatch(f"reviewed plan no longer validates: {exc}") from exc
    if compiled.plan_hash != plan_hash:
        raise ReviewMismatch("reviewed plan no longer matches plan_hash")
    return _finish(
        question,
        principal=principal,
        config=config,
        scoped=scoped,
        plan=None,
        compiled=compiled,
        explanation=_SQL_EXPLANATION,
        answer_plan=None,
        retrieved=[],
        confirm=False,
        options=options,
        review=review,
        policy_checks=SQL_PATH_CHECKS,
        timer=timer,
    )


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


def _second_reader(client: LLMClient) -> LLMClient | None:
    """The client that reads the question a second time for the self-consistency gate.

    A real model client supplies its own sampling twin. A client without one (a scripted
    test client) runs without the gate.
    """
    twin = getattr(client, "check_twin", None)
    return twin() if callable(twin) else None


def production_auth_blocked() -> str | None:
    """Dev identity is forbidden when SECURE_QUERY_ENV=production."""
    env = (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower()
    mode = (os.environ.get("SECURE_QUERY_AUTH_MODE") or "").strip().lower()
    if env == "production" and mode == "dev":
        return "SECURE_QUERY_AUTH_MODE=dev is not allowed when SECURE_QUERY_ENV=production"
    return None
