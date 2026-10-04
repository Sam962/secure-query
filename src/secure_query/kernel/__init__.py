"""Trust kernel: LogicalPlan IR, catalog, validate, AST compile.

This package is the security boundary. The LLM never lives here.
"""

from secure_query.kernel.builder import LQP
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, Synonym, TableSpec
from secure_query.kernel.compile import CompilationError, CompiledQuery, compile
from secure_query.kernel.errors import ValidationError
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.validate import validate, validate_and_compile

__all__ = [
    "Catalog",
    "ColumnSpec",
    "CompilationError",
    "CompiledQuery",
    "JoinKey",
    "LQP",
    "LogicalPlan",
    "Synonym",
    "TableSpec",
    "ValidationError",
    "compile",
    "explain_plan",
    "validate",
    "validate_and_compile",
]
