"""Secure deterministic SQL path: LogicalPlan -> validate(catalog) -> compile(AST) -> execute.

Public API:
    validate(plan, catalog) -> list[ValidationError]
    compile(plan) -> CompiledQuery
    validate_and_compile(plan, catalog) -> CompiledQuery
    plan_question(question, catalog, client) -> PlannerResult
    execute_duckdb(compiled, db_path, ...) -> ExecutionResult

The LLM must only emit LogicalPlan JSON. It must never emit SQL.
"""

from secure_query.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.compile import CompilationError, CompiledQuery, compile
from secure_query.errors import ValidationError
from secure_query.execute import (
    AuditRecord,
    ExecuteOptions,
    ExecutionError,
    ExecutionResult,
    execute_duckdb,
)
from secure_query.databricks import draft_catalog, execute_databricks
from secure_query.logical_plan import LogicalPlan
from secure_query.planner import (
    LLMClient,
    MockLLMClient,
    OpenAIClient,
    PlannerResult,
    default_client,
    plan_question,
    resolve_llm_settings,
)
from secure_query.validate import validate, validate_and_compile

__all__ = [
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
    "compile",
    "default_client",
    "draft_catalog",
    "execute_databricks",
    "execute_duckdb",
    "plan_question",
    "resolve_llm_settings",
    "validate",
    "validate_and_compile",
]
