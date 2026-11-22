"""What the SQL planner returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.metrics import MetricSpec
from secure_query.planner.clarify import ClarifyCode


@dataclass
class PlannerResult:
    """Outcome of plan_sql_question."""

    status: Literal["ok", "clarify"]
    question: str
    plan: LogicalPlan | None = None
    compiled: CompiledQuery | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    clarify_message: str | None = None
    raw_responses: list[str] = field(default_factory=list)
    refused: bool = False
    """True when the model deliberately declined, vs. failing to produce a valid query."""
    clarify_code: ClarifyCode | None = None
    metric: MetricSpec | None = None
    refused_by_model: bool = False
    """The model declined. clarify_code is inferred from its wording (for the UI),
    so it must not be credited to a deterministic guard in eval scorecards."""
    source_sql: str | None = None
    """The model's SQL that validated (re-validated by /ask/execute)."""
    blocked: CompiledQuery | None = None
    """A valid query a guard refused. Never executed by the product; the eval harness
    runs it to score whether the guard blocked a right or wrong answer."""
    gate_fingerprints: tuple[str, ...] = ()
    """Execution fingerprints from the self-consistency gate."""
    certified_id: str | None = None
    """The analyst-approved answer the question was routed to, if any."""
