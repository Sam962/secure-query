"""Certified answers: analyst-approved question/SQL pairs the planner may route to."""

from __future__ import annotations

import json

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.catalog import CertifiedQuery
from secure_query.planner.sql_plan import build_sql_prompt, plan_sql_question

APPROVED = 'SELECT "Country", COUNT(*) AS customers FROM "Customer" GROUP BY "Country"'
CATALOG = sample_catalog().model_copy(
    update={"certified": [CertifiedQuery(id="customers_by_country", question="How many customers per country?",
                                         sql=APPROVED)]}
)


class Replies:
    def __init__(self, *replies: dict) -> None:
        self.replies = [json.dumps(r) for r in replies]
        self.sent: list[str] = []

    def complete(self, messages, response_format=None) -> str:
        self.sent.append(messages[-1]["content"])
        return self.replies.pop(0)


def test_prompt_lists_certified_answers_only_when_there_are_some() -> None:
    assert "customers_by_country: How many customers per country?" in build_sql_prompt("q", CATALOG)
    assert '{"certified": "<id>"}' in build_sql_prompt("q", CATALOG)
    assert "certified" not in build_sql_prompt("q", sample_catalog())


def test_a_routed_question_runs_the_approved_sql() -> None:
    result = plan_sql_question("Count customers in each country", CATALOG, Replies({"certified": "customers_by_country"}))
    assert result.status == "ok" and result.certified_id == "customers_by_country"
    assert result.source_sql == APPROVED
    assert '"Country"' in result.compiled.sql and "COUNT(*)" in result.compiled.sql


def test_an_unknown_id_is_sent_back_for_repair() -> None:
    client = Replies({"certified": "nope"}, {"sql": 'SELECT COUNT(*) AS n FROM "Customer"'})
    result = plan_sql_question("How many customers?", CATALOG, client)
    assert result.status == "ok" and result.certified_id is None
    assert "no certified answer has id 'nope'" in client.sent[-1]


def test_certified_sql_still_goes_through_the_kernel() -> None:
    leaky = sample_catalog().model_copy(
        update={"certified": [CertifiedQuery(id="emails", question="Customer emails?", sql='SELECT "Email" FROM "Customer"')]}
    )
    result = plan_sql_question("Customer emails?", leaky, Replies({"certified": "emails"}, {"certified": "emails"}),
                               max_repairs=1)
    assert result.status == "clarify" and result.compiled is None


def test_certified_ids_are_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        sample_catalog().model_copy(update={"certified": [CERT, CERT]}).model_validate(
            sample_catalog().model_copy(update={"certified": [CERT, CERT]}).model_dump()
        )


CERT = CertifiedQuery(id="a", question="q", sql="SELECT 1")


def test_a_person_sees_only_certified_answers_over_tables_they_may_read() -> None:
    from secure_query.auth import Principal, catalog_for_principal

    both = CATALOG.model_copy(update={"certified": [
        *CATALOG.certified,
        CertifiedQuery(id="invoices", question="How many invoices?", sql='SELECT COUNT(*) FROM "Invoice"'),
    ]})
    scoped = catalog_for_principal(both, Principal(principal_id="p", tenant_id=both.tenant_id,
                                                   allowed_tables=frozenset({"Customer"})))
    assert [c.id for c in scoped.certified] == ["customers_by_country"]


def test_the_answer_says_it_is_certified() -> None:
    from secure_query.api.service import ask
    from secure_query.auth import Principal
    from secure_query.engine.runtime import runtime_config

    outcome = ask("Count customers in each country", principal=Principal(principal_id="p", tenant_id=CATALOG.tenant_id),
                  config=runtime_config(), catalog=CATALOG, client=Replies({"certified": "customers_by_country"}),
                  confirm_only=True)
    assert outcome.status == "confirm"
    assert outcome.explanation == "Certified answer customers_by_country: How many customers per country?"
