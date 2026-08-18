"""Template answers without sending result cells to an LLM."""

from __future__ import annotations

from secure_query.execute import ExecutionResult
from secure_query.explain import explain_plan
from secure_query.logical_plan import LogicalPlan


def format_template_answer(
    question: str,
    plan: LogicalPlan | None,
    result: ExecutionResult,
    *,
    max_rows_show: int = 10,
) -> str:
    """Deterministic answer: explain-back + small result table. No LLM."""
    lines = [f"Question: {question}"]
    if plan is not None:
        lines.append(f"Plan: {explain_plan(plan)}")
    lines.append(f"Rows returned: {len(result.rows)}" + (" (truncated)" if result.truncated else ""))
    if not result.rows:
        lines.append("No matching rows.")
        return "\n".join(lines)
    header = " | ".join(result.columns)
    lines.append(header)
    lines.append("-" * min(len(header), 80))
    for row in result.rows[:max_rows_show]:
        lines.append(" | ".join(str(c) for c in row))
    if len(result.rows) > max_rows_show:
        lines.append(f"... and {len(result.rows) - max_rows_show} more rows")
    return "\n".join(lines)
