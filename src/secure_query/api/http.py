"""Minimal HTTP API for secure query.

Identity is never taken from the JSON body. Resolve it from:
  - SECURE_QUERY_AUTH_MODE=dev   (local: env principal)
  - SECURE_QUERY_AUTH_MODE=token (Authorization: Bearer)
  - SECURE_QUERY_AUTH_MODE=header (X-Forwarded-User / X-Databricks-User from SSO)

Execute backend: DuckDB locally, Databricks SQL warehouse, or Postgres.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from secure_query.api.service import ask, production_auth_blocked
from secure_query.auth import AuthError, resolve_principal
from secure_query.engine.execute import ExecuteOptions, ExecutionError
from secure_query.engine.runtime import load_active_catalog, runtime_config
from secure_query.planner import default_client
from secure_query.planner.suggest import SuggestedQuestion


def _ui_index() -> Path:

    return Path(__file__).resolve().parent / "static" / "index.html"

app = FastAPI(
    title="Secure Query API",
    version="0.4.0",
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
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ui_index())


@app.get("/health")
def health() -> dict[str, str]:
    config = runtime_config()
    return {"status": "ok", "backend": config.backend}


@app.get("/ready")
def ready() -> dict[str, str | bool]:
    blocked = production_auth_blocked()
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    config = runtime_config()
    duckdb_ok = config.backend != "duckdb" or config.duckdb_path.exists()
    if not duckdb_ok:
        raise HTTPException(
            status_code=503,
            detail=f"DuckDB file missing at {config.duckdb_path}",
        )
    return {"status": "ready", "backend": config.backend, "catalog_tenant": config.catalog.tenant_id}


def _run_ask(
    req: AskRequest,
    authorization: str | None,
    x_forwarded_user: str | None,
    x_databricks_user: str | None,
) -> AskResponse:
    blocked = production_auth_blocked()
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    try:
        principal = resolve_principal(
            authorization=authorization,
            forwarded_user=x_forwarded_user or x_databricks_user,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    config = runtime_config()
    catalog = config.catalog
    if req.domain:
        try:
            catalog = load_active_catalog(req.domain)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        outcome = ask(
            req.question,
            principal=principal,
            config=config,
            catalog=catalog,
            client=default_client(),
            confirm_only=req.confirm_only,
            execute=req.execute,
            options=ExecuteOptions(audit_path=config.audit_path),
        )
    except ExecutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _outcome_to_response(outcome)


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(
    req: AskRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    return _run_ask(req, authorization, x_forwarded_user, x_databricks_user)


@app.post("/ask/confirm", response_model=AskResponse)
def ask_confirm(
    req: AskRequest,
    authorization: str | None = Header(default=None),
    x_forwarded_user: str | None = Header(default=None),
    x_databricks_user: str | None = Header(default=None),
) -> AskResponse:
    req = req.model_copy(update={"confirm_only": True, "execute": False})
    return _run_ask(req, authorization, x_forwarded_user, x_databricks_user)
