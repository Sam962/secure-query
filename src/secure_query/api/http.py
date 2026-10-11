"""Minimal HTTP API for secure query.

Identity is never taken from the JSON body. Resolve it from:
  - SECURE_QUERY_AUTH_MODE=dev   (local: env principal)
  - SECURE_QUERY_AUTH_MODE=token (Authorization: Bearer)
  - SECURE_QUERY_AUTH_MODE=header (X-Forwarded-User / X-Databricks-User from SSO)

Execute backend: DuckDB locally, Databricks SQL warehouse, or Postgres.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from secure_query.api.service import (
    ReviewForbidden,
    ReviewMismatch,
    ask,
    execute_reviewed,
    production_auth_blocked,
)
from secure_query.auth import AuthError, check_auth_config, resolve_principal
from secure_query.engine.databricks import databricks_grant_check
from secure_query.engine.env import load_dotenv
from secure_query.engine.execute import ExecuteOptions, ExecutionError
from secure_query.engine.runtime import get_runtime, load_active_catalog
from secure_query.planner import PlannerError, get_client
from secure_query.planner.suggest import SuggestedQuestion


def _ui_index() -> Path:

    return Path(__file__).resolve().parent / "static" / "index.html"

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    load_dotenv()
    check_auth_config()  # refuse to start with an unsafe auth configuration
    yield


def _package_version() -> str:
    try:
        return pkg_version("secure-query")
    except Exception:  # noqa: BLE001 — uninstalled source tree
        return "0.0.0"


app = FastAPI(
    lifespan=_lifespan,
    title="Secure Query API",
    version=_package_version(),
    description="Governed talk-to-data: LogicalPlan → validate → AST compile → execute.",
)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    confirm_only: bool = Field(
        default=False,
        description="If true, return explain-back without executing SQL",
    )
    execute: bool = Field(default=True, description="Run SQL after plan validates")
    domain: str | None = Field(
        default=None,
        description="Optional domain id; default is SECURE_QUERY_DOMAIN / chinook",
    )


class AskResponse(BaseModel):
    status: str
    question: str
    explanation: str | None = None
    sql: str | None = None
    answer: str | None = None
    clarify_message: str | None = None
    clarify_code: str | None = None
    refused: bool = False
    audit: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    retrieved_tables: list[str] = Field(default_factory=list)
    suggestions: list[SuggestedQuestion] = Field(default_factory=list)
    column_units: list[str | None] = Field(default_factory=list)
    scale_note: str | None = None
    review: dict[str, Any] | None = Field(
        default=None,
        description="Send back to /ask/execute to run exactly this reviewed query",
    )


class ExecuteRequest(BaseModel):
    """The `review` payload from /ask/confirm. Re-validated server-side; no LLM call."""

    model_config = ConfigDict(extra="forbid")

    plan_hash: str
    plan: dict[str, Any] | None = None
    sql: str | None = None
    metric_id: str | None = None
    signature: str = ""
    question: str = ""
    domain: str | None = None


def _outcome_to_response(outcome) -> AskResponse:
    return AskResponse(
        status=outcome.status,
        question=outcome.question,
        explanation=outcome.explanation,
        sql=outcome.sql,
        answer=outcome.answer,
        clarify_message=outcome.clarify_message,
        clarify_code=outcome.clarify_code,
        refused=outcome.refused,
        audit=outcome.audit,
        columns=outcome.columns,
        rows=outcome.rows,
        retrieved_tables=outcome.retrieved_tables,
        suggestions=list(outcome.suggestions),
        column_units=list(outcome.column_units),
        scale_note=outcome.scale_note,
        review=outcome.review,
    )


@app.exception_handler(ExecutionError)
def _execution_failed(_request: Request, exc: ExecutionError) -> JSONResponse:
    """Database error text (table names, paths) stays in the server log and audit."""
    log.warning("execute failed (audit_id=%s): %s", exc.audit_id, exc)
    reference = f" Reference: {exc.audit_id}" if exc.audit_id else ""
    return JSONResponse(
        status_code=503, content={"detail": f"The query could not be executed.{reference}"}
    )


@app.exception_handler(PlannerError)
def _planner_failed(_request: Request, exc: PlannerError) -> JSONResponse:
    reference = uuid.uuid4().hex
    log.error("planner failed (reference=%s): %s", reference, exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"The query planner is unavailable. Reference: {reference}"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ui_index())


@app.get("/health")
def health() -> dict[str, str]:
    config = get_runtime()
    return {"status": "ok", "backend": config.backend}


@app.get("/ready")
def ready() -> dict[str, str | bool]:
    """Unauthenticated: reports readiness only, never the tenant or file paths."""
    blocked = production_auth_blocked()
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    config = get_runtime()
    if config.backend == "duckdb" and not config.duckdb_path.exists():
        log.error("DuckDB file missing at %s", config.duckdb_path)
        raise HTTPException(status_code=503, detail="database not available")
    body: dict[str, str | bool] = {"status": "ready", "backend": config.backend}
    if config.backend == "databricks":
        try:
            grants = databricks_grant_check()
        except Exception as exc:  # noqa: BLE001 — readiness must answer, not crash
            log.error("databricks grant check failed: %s", exc)
            raise HTTPException(status_code=503, detail="warehouse grant check failed") from exc
        body["databricks_grants"] = grants["status"]
        if grants["status"] == "write_privileges":
            log.error("databricks grant check: %s", grants["detail"])
            raise HTTPException(
                status_code=503, detail="warehouse principal has write privileges; expected SELECT-only"
            )
    return body


def _principal_and_catalog(
    request: Request,
    domain: str | None,
    authorization: str | None,
    x_forwarded_user: str | None,
    x_databricks_user: str | None,
):
    blocked = production_auth_blocked()
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    try:
        principal = resolve_principal(
            authorization=authorization,
            forwarded_user=x_forwarded_user or x_databricks_user,
            client_host=request.client.host if request.client else None,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    config = get_runtime()
    catalog = config.catalog
    if domain:
        try:
            catalog = load_active_catalog(domain)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return principal, config, catalog


def _run_ask(
    request: Request,
    req: AskRequest,
    authorization: str | None,
    x_forwarded_user: str | None,
    x_databricks_user: str | None,
) -> AskResponse:
    principal, config, catalog = _principal_and_catalog(
        request, req.domain, authorization, x_forwarded_user, x_databricks_user
    )
    outcome = ask(
        req.question,
        principal=principal,
        config=config,
        catalog=catalog,
        client=get_client(),
        confirm_only=req.confirm_only,
        execute=req.execute,
        options=ExecuteOptions(audit_path=config.audit_path),
    )
    return _outcome_to_response(outcome)


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(
    request: Request,
    req: AskRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    return _run_ask(request, req, authorization, x_forwarded_user, x_databricks_user)


@app.post("/ask/confirm", response_model=AskResponse)
def ask_confirm(
    request: Request,
    req: AskRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    req = req.model_copy(update={"confirm_only": True, "execute": False})
    return _run_ask(request, req, authorization, x_forwarded_user, x_databricks_user)


@app.post("/ask/execute", response_model=AskResponse)
def ask_execute(
    request: Request,
    req: ExecuteRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    """Run the query reviewed via /ask/confirm. 409 if it no longer compiles to plan_hash."""
    principal, config, catalog = _principal_and_catalog(
        request, req.domain, authorization, x_forwarded_user, x_databricks_user
    )
    try:
        outcome = execute_reviewed(
            req.question,
            principal=principal,
            config=config,
            catalog=catalog,
            plan_hash=req.plan_hash,
            plan=req.plan,
            sql=req.sql,
            metric_id=req.metric_id,
            signature=req.signature,
            options=ExecuteOptions(audit_path=config.audit_path),
        )
    except ReviewForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ReviewMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _outcome_to_response(outcome)
