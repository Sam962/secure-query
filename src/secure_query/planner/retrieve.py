"""Lexical table retrieval for planner prompts.

Retrieval shrinks the prompt. It is never a security control: validate() must
still run against the full authorized catalog so a miss becomes a refusal,
not a leak.
"""

from __future__ import annotations

import os
import re

from secure_query.auth import Principal, catalog_for_principal
from secure_query.kernel.catalog import Catalog

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "for",
        "in",
        "on",
        "by",
        "to",
        "from",
        "what",
        "which",
        "how",
        "many",
        "show",
        "list",
        "me",
        "is",
        "are",
        "was",
        "were",
        "with",
        "our",
        "per",
        "each",
        "all",
        "top",
    }
)


def retrieve_k_from_env(table_count: int) -> int:
    """0 = off. Auto-enable once a catalog is wide enough that prompt size matters."""
    raw = (os.environ.get("SECURE_QUERY_RETRIEVE_K") or "").strip()
    if raw:
        return max(0, int(raw))
    if table_count > 16:
        return 8
    return 0


def tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1}


def retrieve_tables(question: str, catalog: Catalog, *, k: int = 8) -> list[str]:
    """Rank tables by lexical overlap with the question; expand one join hop."""
    if k <= 0 or not catalog.tables:
        return [t.name for t in catalog.tables]
    q_tokens = tokenize(question)
    scores: dict[str, int] = {t.name: 0 for t in catalog.tables}
    for table in catalog.tables:
        blob = " ".join(
            [table.name, table.description]
            + [c.name for c in table.columns]
            + [c.description for c in table.columns]
        )
        overlap = len(q_tokens & tokenize(blob))
        if table.name.lower() in question.lower():
            overlap += 4
        scores[table.name] = overlap
    for syn in catalog.synonyms:
        term = syn.term.lower()
        if term and term in question.lower():
            scores[syn.table_id] = scores.get(syn.table_id, 0) + 3
    ranked = sorted(scores, key=lambda name: (-scores[name], name))
    chosen = [name for name in ranked if scores[name] > 0][:k]
    if not chosen:
        return [t.name for t in catalog.tables]
    return _expand_join_neighbors(catalog, chosen)


def prompt_token_budget() -> int:
    """Catalog summary size (approx. tokens) above which the prompt is cut to the retrieval slice."""
    raw = (os.environ.get("SECURE_QUERY_PROMPT_CATALOG_TOKENS") or "").strip()
    return int(raw) if raw else 24_000


def prompt_catalog_and_hint(catalog: Catalog, retrieved: list[str]) -> tuple[Catalog, list[str]]:
    """The catalog to put in the prompt, and the "likely relevant tables" hint.

    The full catalog is the stable prompt block, identical for every question, so
    the provider's prompt cache covers it; retrieval only adds a hint after it.
    The catalog is cut to the retrieval slice only when it exceeds the budget.
    """
    narrowed = retrieved if retrieved and len(set(retrieved)) < len(catalog.tables) else []
    if len(catalog.planner_summary()) / 4 <= prompt_token_budget():
        return catalog, narrowed
    return (catalog_for_prompt(catalog, narrowed) if narrowed else catalog), []


def catalog_for_prompt(catalog: Catalog, table_ids: list[str]) -> Catalog:
    """Prompt-only slice. Caller must still validate against `catalog`."""
    allowed = frozenset(table_ids)
    if not allowed:
        return catalog
    return catalog_for_principal(
        catalog,
        Principal(
            principal_id="prompt-retrieval",
            tenant_id=catalog.tenant_id,
            allowed_tables=allowed,
        ),
    )


def _expand_join_neighbors(catalog: Catalog, table_ids: list[str]) -> list[str]:
    chosen = set(table_ids)
    for jk in catalog.join_keys:
        if jk.left_table in chosen:
            chosen.add(jk.right_table)
        if jk.right_table in chosen:
            chosen.add(jk.left_table)
    order = [t.name for t in catalog.tables if t.name in chosen]
    return order
