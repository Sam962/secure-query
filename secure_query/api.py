"""Minimal HTTP API for secure query.

Identity is never taken from the JSON body. Resolve it from:
  - SECURE_QUERY_AUTH_MODE=dev   (local: env principal)
  - SECURE_QUERY_AUTH_MODE=token (Authorization: Bearer)
  - SECURE_QUERY_AUTH_MODE=header (X-Forwarded-User / X-Databricks-User from SSO)

Execute backend: DuckDB locally, or Databricks SQL warehouse when DATABRICKS_*
env vars are set. Catalog: SECURE_QUERY_CATALOG_FILE or Chinook sample.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from secure_query.auth import (
    AuthError,
    assert_ratio_allowed,
    audit_principal_fields,
    catalog_for_principal,
    inject_row_filters,
    resolve_principal,
)
from secure_query.execute import ExecuteOptions, ExecutionError
from secure_query.explain import explain_plan
from secure_query.planner import default_client, plan_question
from secure_query.respond import format_template_answer
from secure_query.runtime import execute_compiled_query, runtime_config
from secure_query.validate import PlanValidationFailed, validate_and_compile

app = FastAPI(title="Secure Query API", version="0.3.0")


class AskRequest(BaseModel):
    question: str
    confirm_only: bool = Field(
        default=False,
        description="If true, return explain-back without executing SQL",
    )
    execute: bool = Field(default=True, description="Run SQL after plan validates")


class AskResponse(BaseModel):
    status: str
    question: str
    explanation: str | None = None
    sql: str | None = None
    answer: str | None = None
    clarify_message: str | None = None
    refused: bool = False
    audit: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, str]:
    config = runtime_config()
    return {"status": "ok", "backend": config.backend}


@app.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    try:
        principal = resolve_principal(
            authorization=authorization,
            forwarded_user=x_forwarded_user or x_databricks_user,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    config = runtime_config()
    try:
        catalog = catalog_for_principal(config.catalog, principal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    client = default_client()
    planned = plan_question(req.question, catalog, client, max_repairs=1)

    if planned.status != "ok" or (planned.compiled is None and planned.plan is None):
        return AskResponse(
            status="clarify",
            question=req.question,
            clarify_message=planned.clarify_message,
            refused=planned.refused,
            audit=audit_principal_fields(principal),
        )

    plan = planned.plan
    compiled = planned.compiled

    if plan is not None:
        plan = inject_row_filters(plan, principal)
        try:
            compiled = validate_and_compile(plan, catalog)
        except PlanValidationFailed as exc:
            return AskResponse(
                status="clarify",
                question=req.question,
                clarify_message=str(exc),
                audit=audit_principal_fields(principal),
            )
    elif compiled is None:
        return AskResponse(
            status="clarify",
            question=req.question,
            clarify_message="No plan or compiled SQL produced",
            audit=audit_principal_fields(principal),
        )
    else:
        try:
            assert_ratio_allowed(principal)
        except PermissionError as exc:
            return AskResponse(
                status="clarify",
                question=req.question,
                clarify_message=str(exc),
                refused=True,
                audit=audit_principal_fields(principal),
            )

    explanation = explain_plan(plan) if plan else "Approved ratio metric"
    assert compiled is not None

    audit_base = {
        **audit_principal_fields(principal),
        "backend": config.backend,
        "plan_hash": compiled.plan_hash,
    }

    if req.confirm_only or not req.execute:
        return AskResponse(
            status="confirm",
            question=req.question,
            explanation=explanation,
            sql=compiled.sql,
            audit=audit_base,
        )

    try:
        result = execute_compiled_query(
            compiled,
            config,
            plan=plan,
            question=req.question,
            principal=principal,
            options=ExecuteOptions(
                audit_path=config.audit_path,
            ),
        )
    except ExecutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    answer = format_template_answer(req.question, plan, result)

    return AskResponse(
        status="ok",
        question=req.question,
        explanation=explanation,
        sql=compiled.sql,
        answer=answer,
        audit={
            **audit_base,
            "sql_hash": compiled.sql_hash,
            "row_count": len(result.rows),
        },
    )
