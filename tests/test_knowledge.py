"""Knowledge overlay cannot inject SQL examples."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.planner.knowledge import KnowledgeError, apply_knowledge, load_knowledge_file


def test_overlay_adds_synonym_and_instruction() -> None:
    catalog = sample_catalog()
    out = apply_knowledge(
        catalog,
        {
            "instructions": ["Prefer Customer.Country when the question says country."],
            "synonyms": [{"term": "buyer", "table_id": "Customer"}],
        },
    )
    assert any(s.term == "buyer" for s in out.synonyms)
    assert out.instructions[-1].startswith("Prefer Customer.Country")
    assert "instructions" in out.planner_summary()


def test_knowledge_file_rejects_sql_examples(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"examples": [{"sql": "SELECT 1"}]}), encoding="utf-8")
    with pytest.raises(KnowledgeError, match="SQL"):
        load_knowledge_file(path)


def test_instructions_cannot_contain_select() -> None:
    with pytest.raises(KnowledgeError, match="SQL"):
        apply_knowledge(sample_catalog(), {"instructions": ["Always SELECT *"]})
