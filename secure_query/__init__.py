"""Secure deterministic SQL path: LogicalPlan -> validate(catalog) -> compile(AST).

Public API:
    validate(plan, catalog) -> list[ValidationError]
    compile(plan) -> CompiledQuery
    validate_and_compile(plan, catalog) -> CompiledQuery

The LLM must only emit LogicalPlan JSON. It must never emit SQL.
"""

from secure_query.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.compile import CompilationError, CompiledQuery, compile
from secure_query.errors import ValidationError
from secure_query.logical_plan import LogicalPlan
from secure_query.validate import validate, validate_and_compile

__all__ = [
    "Catalog",
    "ColumnSpec",
    "CompilationError",
    "CompiledQuery",
    "JoinKey",
    "LogicalPlan",
    "TableSpec",
    "ValidationError",
    "compile",
    "validate",
    "validate_and_compile",
]
