"""Catalog-grounded clarify suggestions — never a silent rewrite."""

from __future__ import annotations

from secure_query.auth import Principal
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner import MockLLMClient
from secure_query.planner.suggest import suggest_questions
from secure_query.engine.runtime import runtime_config
from secure_query.api.service import ask


def test_sql_paste_has_no_suggestions() -> None:
    catalog = sample_catalog()
    assert suggest_questions("SELECT * FROM Customer", catalog, code="sql_in_question") == []
    outcome = ask(
        "SELECT email FROM Customer",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=catalog,
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.clarify_code == "sql_in_question"
    assert outcome.suggestions == []


def test_pii_and_handoff_and_out_of_scope_have_no_suggestions() -> None:
    catalog = sample_catalog()
    assert suggest_questions("list all emails", catalog, code="restricted_pii") == []
    assert suggest_questions(
        "What was the percentage growth in revenue from 2023 to 2024?",
        catalog,
        code="analyst_handoff",
    ) == []
    assert (
        suggest_questions(
            "How much did we spend with our suppliers last quarter?",
            catalog,
            code="out_of_scope",
        )
        == []
    )


def test_spend_synonym_is_not_rewritten_into_revenue() -> None:
    """Column synonyms must not turn an unknown domain into the nearest metric."""
    catalog = sample_catalog()
    suggestions = suggest_questions(
        "How much did we spend with our suppliers last quarter?",
        catalog,
        code="validation_failed",
    )
    joined = " ".join(s.question.lower() for s in suggestions)
    assert "supplier" not in joined or suggestions == []
    assert not any("invoice" in s.question.lower() and "total" in s.question.lower() for s in suggestions)


def test_table_synonym_rewrites_client_to_customer() -> None:
    catalog = sample_catalog()
    suggestions = suggest_questions(
        "How many clients are in the USA?",
        catalog,
        code="validation_failed",
    )
    assert any("customer" in s.question.lower() for s in suggestions)
    assert all(s.question.lower() != "how many clients are in the usa?" for s in suggestions)


def test_empty_question_offers_metric_starters() -> None:
    outcome = ask(
        "  ",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.clarify_code == "invalid_request"
    assert len(outcome.suggestions) == 3
    assert all(s.question.endswith("?") for s in outcome.suggestions)


def test_suggestions_are_askable_and_omit_grouped() -> None:
    """A suggestion that still says 'grouped' would refuse again when clicked."""
    catalog = sample_catalog()
    suggestions = suggest_questions(
        "revenue grouped by country",
        catalog,
        code="planner_refusal",
    )
    from secure_query.planner.guard import out_of_scope_request

    assert suggestions
    for item in suggestions:
        assert "grouped" not in item.question.lower()
        assert out_of_scope_request(item.question, catalog) is None


def test_out_of_scope_message_maps_to_out_of_scope_code() -> None:
    from secure_query.planner.clarify import code_from_guard_message

    code = code_from_guard_message(
        'The question mentions terms not in the approved catalog or synonyms: "grouped".',
        refused=True,
    )
    assert code == "out_of_scope"


def test_ambiguous_metrics_suggest_each_named_metric() -> None:
    catalog = sample_catalog()
    suggestions = suggest_questions(
        "compare total revenue and invoice count",
        catalog,
        code="ambiguous_metric",
    )
    ids = " ".join(s.reason for s in suggestions)
    assert "total_revenue" in ids
    assert "invoice_count" in ids
