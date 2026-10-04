"""Secure deterministic SQL path: LogicalPlan -> validate(catalog) -> compile(AST) -> execute.

Public API:
    validate(plan, catalog) -> list[ValidationError]
    compile(plan) -> CompiledQuery
    validate_and_compile(plan, catalog) -> CompiledQuery
    plan_question(question, catalog, client) -> PlannerResult
    execute_duckdb(compiled, db_path, ...) -> ExecutionResult

The LLM must only emit LogicalPlan JSON. It must never emit SQL.
"""

from secure_query.api.service import AskOutcome, ask
from secure_query.engine.databricks import draft_catalog, execute_databricks
from secure_query.engine.execute import (
    AuditRecord,
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    execute_duckdb,
)
from secure_query.engine.postgres import execute_postgres
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.kernel.compile import CompilationError, CompiledQuery, compile
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.validate import validate, validate_and_compile
from secure_query.planner import (
    LLMClient,
    MockLLMClient,
    OpenAIClient,
    PlannerResult,
    default_client,
    plan_question,
    resolve_llm_settings,
)

__all__ = [
    "AskOutcome",
    "AuditRecord",
    "Catalog",
    "ColumnSpec",
    "CompilationError",
    "CompiledQuery",
    "ExecuteOptions",
    "ExecutionError",
    "ExecutionResult",
    "JoinKey",
    "LLMClient",
    "LogicalPlan",
    "MockLLMClient",
    "OpenAIClient",
    "PlannerResult",
    "TableSpec",
    "ValidationError",
    "ask",
    "compile",
    "default_client",
    "draft_catalog",
    "execute_databricks",
    "execute_duckdb",
    "execute_postgres",
    "plan_question",
    "resolve_llm_settings",
    "validate",
    "validate_and_compile",
]
