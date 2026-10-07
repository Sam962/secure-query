"""Structured clarify reasons — API contract for refuse / ask-again / handoff."""

from __future__ import annotations

import re
from typing import Literal

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.metrics import metrics_for_catalog

ClarifyCode = Literal[
    "restricted_pii",
    "out_of_scope",
    "dropped_concept",
    "dropped_filter",
    "useless_join",
    "validation_failed",
    "planner_refusal",
    "ambiguous_metric",
    "sql_in_question",
    "analyst_handoff",
    "auth",
    "invalid_request",
]

_SQL_HEAD = re.compile(
    r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|MERGE|CREATE)\b",
    re.IGNORECASE,
)
_QUESTION_MAX = 2000

_HANDOFF_MARKERS = (
    "ratio",
    "percentage",
    "share of",
    "growth rate",
    "window",
    "subquery",
    "cannot express",
    "no division",
)


def question_too_long(question: str) -> str | None:
    if len(question) > _QUESTION_MAX:
        return f"Question exceeds {_QUESTION_MAX} characters"
    return None


def looks_like_sql_statement(question: str) -> bool:
    """True when the user pasted a SQL statement instead of asking in English."""
    return bool(_SQL_HEAD.match(question or ""))


def ambiguous_metrics(question: str, catalog: Catalog) -> str | None:
    """Clarify when two approved metric ids both appear as phrases in the question."""
    q = " ".join((question or "").lower().split())
    hits = [
        m.id
        for m in metrics_for_catalog(catalog)
        if m.id.replace("_", " ") in q
    ]
    if len(hits) > 1:
        return (
            "Question matches multiple approved metrics ("
            + ", ".join(hits)
            + "). Name one metric, or be more specific."
        )
    return None


def code_from_guard_message(message: str | None, *, refused: bool) -> ClarifyCode | None:
    if not message:
        return None
    lower = message.lower()
    if "restricted" in lower or "pii" in lower:
        return "restricted_pii"
    if (
        "out of scope" in lower
        or "not in the catalog" in lower
        or "not in the approved catalog" in lower
        or "approved catalog or synonyms" in lower
    ):
        return "out_of_scope"
    if "no filter in this plan" in lower:
        return "dropped_filter"
    if "useless" in lower and "join" in lower:
        return "useless_join"
    if "dropped" in lower or "never used" in lower or "never returns" in lower or "named" in lower:
        return "dropped_concept"
    if any(marker in lower for marker in _HANDOFF_MARKERS):
        return "analyst_handoff"
    if refused:
        return "planner_refusal"
    return "validation_failed"
