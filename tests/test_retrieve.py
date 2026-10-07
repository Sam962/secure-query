"""Retrieval is a prompt optimization, never the validate allowlist."""

from __future__ import annotations

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.validate import validate_and_compile
from secure_query.planner import MockLLMClient, plan_question
from secure_query.planner.retrieve import catalog_for_prompt, retrieve_tables


def test_genre_question_retrieves_genre_and_neighbors() -> None:
    catalog = sample_catalog()
    tables = retrieve_tables("top music genre by revenue", catalog, k=4)
    assert "Genre" in tables
    assert "Track" in tables or "InvoiceLine" in tables


def test_prompt_slice_omits_unrelated_tables() -> None:
    catalog = sample_catalog()
    sliced = catalog_for_prompt(catalog, ["Invoice", "Customer"])
    names = {t.name for t in sliced.tables}
    assert names == {"Invoice", "Customer"}
    assert "Employee" not in names


def test_validation_still_uses_full_catalog_when_prompt_is_sliced() -> None:
    catalog = sample_catalog()
    prompt = catalog_for_prompt(catalog, ["Invoice"])
    result = plan_question(
        "revenue by country",
        catalog,
        MockLLMClient(),
        prompt_catalog=prompt,
    )
    assert result.status == "ok"
    assert result.plan is not None
    assert result.plan.source == "Invoice"
    compiled = validate_and_compile(result.plan, catalog)
    assert "Customer" in compiled.sql


def test_two_questions_share_the_prompt_prefix_through_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieval adds a hint after the catalog; it never rewrites the cacheable block."""
    from secure_query.api.service import ask
    from secure_query.auth import Principal
    from secure_query.demo.chinook import sample_catalog
    from secure_query.engine.runtime import runtime_config
    from secure_query.planner import MockLLMClient

    monkeypatch.setenv("SECURE_QUERY_RETRIEVE_K", "1")
    prompts = []
    for question in ("revenue by genre", "how many employees are there"):
        client = MockLLMClient()
        ask(
            question,
            principal=Principal(principal_id="u", tenant_id="chinook"),
            config=runtime_config(),
            catalog=sample_catalog(),
            client=client,
            confirm_only=True,
        )
        prompts.append(client.calls[0][1]["content"])
    catalog_block = (
        "Approved catalog (tables/columns/joins only — no row data):\n"
        f"{sample_catalog().planner_summary()}\n\n"
    )
    assert all(p.startswith(catalog_block) for p in prompts)
    assert "Likely relevant tables:" in prompts[0] and prompts[0] != prompts[1]


def test_catalog_is_cut_to_retrieval_only_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.demo.chinook import sample_catalog
    from secure_query.planner.retrieve import prompt_catalog_and_hint

    catalog = sample_catalog()
    full, hint = prompt_catalog_and_hint(catalog, ["Genre", "Track"])
    assert full is catalog and hint == ["Genre", "Track"]
    monkeypatch.setenv("SECURE_QUERY_PROMPT_CATALOG_TOKENS", "10")
    cut, hint = prompt_catalog_and_hint(catalog, ["Genre", "Track"])
    assert {t.name for t in cut.tables} == {"Genre", "Track"} and hint == []
