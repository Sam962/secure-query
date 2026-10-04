"""Retrieval is a prompt optimization, never the validate allowlist."""

from __future__ import annotations

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner import MockLLMClient, plan_question
from secure_query.planner.retrieve import catalog_for_prompt, retrieve_tables
from secure_query.kernel.validate import validate_and_compile


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
