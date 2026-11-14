"""Trust kernel: catalog, SQL allowlist validator, LogicalPlan IR, AST compile, trace, evidence.

This package is the security boundary. The LLM never lives here.
"""

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
    "LogicalPlan",
    "Synonym",
    "TableSpec",
    "ValidationError",
    "compile",
    "explain_plan",
    "validate",
    "validate_and_compile",
]
