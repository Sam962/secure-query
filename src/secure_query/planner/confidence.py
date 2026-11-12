"""Confidence threshold for lexical schema linking on wide catalogs.

When the top table scores barely above the runner-up, the linker is guessing.
Refuse with low_confidence rather than silently planning on the wrong slice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from secure_query.kernel.catalog import Catalog
from secure_query.planner.retrieve import retrieve_tables, tokenize


@dataclass(frozen=True)
class LinkDecision:
    """Which tables a question is about, how sure the linker is, and the runners-up."""

    choice: str | None
    confidence: float
    alternatives: tuple[tuple[str, float], ...]
    tables: tuple[str, ...]


def confidence_threshold() -> float:
    """SECURE_QUERY_LINK_CONFIDENCE; 0 (the default) turns the refusal off.

    Off by default: at 0.55 on a 22-table catalog it refused 17 of 53 answerable
    questions (docs/STATUS.md, 2026-11-11).
    """
    raw = (os.environ.get("SECURE_QUERY_LINK_CONFIDENCE") or "0").strip()
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        return 0.0


def score_tables(question: str, catalog: Catalog, *, k: int = 8) -> list[tuple[str, float]]:
    """Return (table, score) sorted descending. Score is normalized lexical overlap."""
    ranked = retrieve_tables(question, catalog, k=max(k, len(catalog.tables)))
    q_tokens = tokenize(question)
    if not q_tokens:
        return [(name, 0.0) for name in ranked]
    scored: list[tuple[str, float]] = []
    for name in ranked:
        table = catalog.table_map().get(name)
        if table is None:
            continue
        blob = " ".join(
            [table.name, table.description]
            + [c.name for c in table.columns]
            + [c.description for c in table.columns]
        )
        overlap = len(q_tokens & tokenize(blob))
        if table.name.lower() in question.lower():
            overlap += 4
        scored.append((name, float(overlap)))
    scored.sort(key=lambda x: x[1], reverse=True)
    total = sum(s for _, s in scored) or 1.0
    return [(name, s / total) for name, s in scored]


def link_tables(question: str, catalog: Catalog, *, k: int = 8) -> LinkDecision:
    scored = score_tables(question, catalog, k=k)
    if not scored:
        return LinkDecision(choice=None, confidence=0.0, alternatives=(), tables=())
    top_name, top_score = scored[0]
    alts = tuple(scored[:5])
    # Softmax-like margin: confidence is top share; weak when runner-up is close.
    second = scored[1][1] if len(scored) > 1 else 0.0
    margin = top_score - second
    confidence = min(1.0, top_score + 0.5 * margin)
    tables = tuple(n for n, _ in scored[:k]) if k > 0 else tuple(n for n, _ in scored)
    return LinkDecision(
        choice=top_name,
        confidence=confidence,
        alternatives=alts,
        tables=tables,
    )


def low_confidence_message(decision: LinkDecision) -> str:
    alts = ", ".join(f"{n} ({s:.2f})" for n, s in decision.alternatives[:3]) or "none"
    return (
        f"I'm not sure which tables this question is about "
        f"(top={decision.choice!r} confidence={decision.confidence:.2f}; alternatives: {alts}). "
        "Name the table or metric and ask again."
    )
