"""Catalog-grounded follow-up questions after a clarify.

The model never rewrites the user's question. Suggestions are built from the
approved catalog (metrics, table-level synonyms) and the user must still
confirm before anything executes.

Some clarify codes must not offer alternatives: pasted SQL, PII, auth failures,
out-of-scope terms, and analyst handoff (do not approximate a ratio).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.metrics import MetricSpec, metrics_for_catalog
from secure_query.planner.retrieve import tokenize

if TYPE_CHECKING:
    from secure_query.planner.clarify import ClarifyCode

_MAX_SUGGESTIONS = 3
_NO_SUGGEST: frozenset[str] = frozenset(
    {
        "sql_in_question",
        "restricted_pii",
        "auth",
        "out_of_scope",
        "analyst_handoff",
    }
)


class SuggestedQuestion(BaseModel):
    """One alternative the user can choose to ask next."""

    question: str
    reason: str

    model_config = ConfigDict(extra="forbid", frozen=True)


def suggest_questions(
    question: str,
    catalog: Catalog,
    *,
    code: ClarifyCode | str,
) -> list[SuggestedQuestion]:
    """Return up to three catalog-owned questions. Never invents SQL or metrics."""
    if code in _NO_SUGGEST:
        return []

    original = (question or "").strip()
    if code == "invalid_request":
        if not original:
            return _starters(catalog)
        return []

    found: list[SuggestedQuestion] = []
    if code == "ambiguous_metric":
        found.extend(_ambiguous_metric_options(original, catalog))
    else:
        found.extend(_table_synonym_rewrites(original, catalog))
        found.extend(_overlapping_metrics(original, catalog))

    askable = [item for item in found if _askable(item.question, catalog)]
    return _dedupe(askable, skip=original)[:_MAX_SUGGESTIONS]


def _starters(catalog: Catalog) -> list[SuggestedQuestion]:
    out: list[SuggestedQuestion] = []
    for metric in metrics_for_catalog(catalog):
        if metric.kind == "ratio":
            continue
        question = _question_for_metric(metric)
        if not _askable(question, catalog):
            continue
        out.append(
            SuggestedQuestion(
                question=question,
                reason=f"Approved metric {metric.id}",
            )
        )
        if len(out) >= _MAX_SUGGESTIONS:
            break
    return out


def _ambiguous_metric_options(question: str, catalog: Catalog) -> list[SuggestedQuestion]:
    q = " ".join(question.lower().split())
    out: list[SuggestedQuestion] = []
    for metric in metrics_for_catalog(catalog):
        phrase = metric.id.replace("_", " ")
        if phrase and phrase in q:
            out.append(
                SuggestedQuestion(
                    question=_question_for_metric(metric),
                    reason=f"Approved metric {metric.id}",
                )
            )
    return out


def _table_synonym_rewrites(question: str, catalog: Catalog) -> list[SuggestedQuestion]:
    """Replace table-level synonyms (client→customer). Column synonyms are skipped.

    Rewriting 'spend' (Invoice.Total) would turn 'supplier spend' into a revenue
    question — the nearest-table guess this kernel refuses to make.
    """
    table_syns = [s for s in catalog.synonyms if s.column_id is None and len(s.term.strip()) >= 3]
    table_syns.sort(key=lambda s: len(s.term), reverse=True)
    out: list[SuggestedQuestion] = []
    for syn in table_syns:
        pattern = re.compile(rf"\b{re.escape(syn.term)}\b", re.IGNORECASE)
        if not pattern.search(question):
            continue
        replacement = _humanize_table(syn.table_id, plural=syn.term.lower().endswith("s"))
        rewritten = pattern.sub(replacement, question, count=1)
        if rewritten.strip().lower() == question.strip().lower():
            continue
        out.append(
            SuggestedQuestion(
                question=rewritten.strip(),
                reason=f"Catalog synonym {syn.term!r} → {syn.table_id}",
            )
        )
    return out


def _overlapping_metrics(question: str, catalog: Catalog) -> list[SuggestedQuestion]:
    q_tokens = tokenize(question)
    if not q_tokens:
        return []
    scored: list[tuple[int, MetricSpec]] = []
    for metric in metrics_for_catalog(catalog):
        if metric.kind == "ratio":
            continue
        id_tokens = tokenize(metric.id.replace("_", " "))
        desc_tokens = tokenize(metric.description)
        id_overlap = len(q_tokens & id_tokens)
        desc_overlap = len(q_tokens & desc_tokens)
        if id_overlap >= 1 or desc_overlap >= 2:
            scored.append((id_overlap * 2 + desc_overlap, metric))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [
        SuggestedQuestion(
            question=_question_for_metric(metric),
            reason=f"Approved metric {metric.id}",
        )
        for _, metric in scored
    ]


def _question_for_metric(metric: MetricSpec) -> str:
    """Askable phrasing from the metric id — never the description.

    Descriptions contain analyst words like "grouped" that the out-of-scope
    guard treats as unknown catalog terms, so suggesting them loops.
    """
    canned = {
        "total_revenue": "What is total invoice revenue?",
        "invoice_count": "How many invoices are there?",
        "employee_count": "How many employees are there?",
        "revenue_by_country": "What is revenue by country?",
        "revenue_by_billing_country": "What is revenue by billing country?",
        "revenue_by_genre": "What is revenue by genre?",
    }
    if metric.id in canned:
        return canned[metric.id]
    return f"What is {metric.id.replace('_', ' ')}?"


def _askable(question: str, catalog: Catalog) -> bool:
    """True when clicking this suggestion would not immediately refuse again."""
    from secure_query.planner.clarify import looks_like_sql_statement
    from secure_query.planner.guard import out_of_scope_request, restricted_request

    text = (question or "").strip()
    if len(text) < 8:
        return False
    if looks_like_sql_statement(text):
        return False
    if restricted_request(text, catalog) is not None:
        return False
    if out_of_scope_request(text, catalog) is not None:
        return False
    return True


def _humanize_table(name: str, *, plural: bool) -> str:
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip().lower()
    if plural and not spaced.endswith("s"):
        return spaced + "s"
    return spaced


def _dedupe(items: list[SuggestedQuestion], *, skip: str) -> list[SuggestedQuestion]:
    skip_key = skip.strip().lower()
    seen: set[str] = {skip_key} if skip_key else set()
    out: list[SuggestedQuestion] = []
    for item in items:
        key = item.question.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
