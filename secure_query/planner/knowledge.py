"""Analyst-owned knowledge overlay — synonyms and instructions, never SQL.

Unlike Text2SQL knowledge files, this overlay cannot inject example queries.
Those would train the planner to speak SQL. Metrics stay in metrics.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from secure_query.kernel.catalog import Catalog, Synonym

_FORBIDDEN_KEYS = frozenset(
    {
        "sql",
        "examples",
        "example_sql",
        "ground_truth_sql",
        "groundTruthSql",
        "queries",
        "few_shot",
        "fewshot",
    }
)


class KnowledgeError(ValueError):
    """Raised when a knowledge file tries to smuggle SQL or unknown fields."""


def load_knowledge_file(path: str | Path) -> dict[str, Any]:
    """Load JSON knowledge. Rejects SQL-shaped keys."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise KnowledgeError("knowledge file must be a JSON object")
    bad = sorted(k for k in data if k.lower() in _FORBIDDEN_KEYS or k in _FORBIDDEN_KEYS)
    if bad:
        raise KnowledgeError(
            "knowledge file cannot contain SQL examples or query strings: " + ", ".join(bad)
        )
    allowed = {"instructions", "synonyms"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KnowledgeError(f"unknown knowledge keys: {unknown}")
    return data


def apply_knowledge(catalog: Catalog, data: dict[str, Any] | None = None) -> Catalog:
    """Merge instructions + synonyms onto an approved catalog."""
    if data is None:
        return catalog
    extra_syn: list[Synonym] = []
    for raw in data.get("synonyms") or []:
        if not isinstance(raw, dict):
            raise KnowledgeError("each synonym must be an object")
        extra_syn.append(
            Synonym(
                term=str(raw["term"]),
                table_id=str(raw["table_id"]),
                column_id=raw.get("column_id"),
                description=str(raw.get("description") or ""),
            )
        )
    seen = {(s.term.lower(), s.table_id, s.column_id) for s in catalog.synonyms}
    merged_syn = list(catalog.synonyms)
    for syn in extra_syn:
        key = (syn.term.lower(), syn.table_id, syn.column_id)
        if key not in seen:
            merged_syn.append(syn)
            seen.add(key)
    extra_instr = [str(x) for x in (data.get("instructions") or []) if str(x).strip()]
    for rule in extra_instr:
        if "SELECT" in rule.upper() or "INSERT" in rule.upper():
            raise KnowledgeError("instructions cannot contain SQL keywords")
    return catalog.model_copy(
        update={
            "synonyms": merged_syn,
            "instructions": list(catalog.instructions) + extra_instr,
        }
    )


def knowledge_path_from_env() -> Path | None:
    raw = (os.environ.get("SECURE_QUERY_KNOWLEDGE_FILE") or "").strip()
    return Path(raw) if raw else None


def overlay_from_env(catalog: Catalog) -> Catalog:
    path = knowledge_path_from_env()
    if path is None:
        return catalog
    if not path.exists():
        raise KnowledgeError(f"knowledge file not found: {path}")
    return apply_knowledge(catalog, load_knowledge_file(path))
