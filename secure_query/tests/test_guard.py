"""Tests for the deterministic question guards.

These guards trade coverage for safety, so the tests care as much about what
they *don't* refuse as what they do — a guard that fires on ordinary questions
would quietly gut the product.
"""

from __future__ import annotations

import pytest

from secure_query.kernel.builder import LQP
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner.guard import (
    Concept,
    dropped_concepts,
    plan_concepts,
    restricted_request,
)

CATALOG = sample_catalog()


class TestRestrictedRequest:
    @pytest.mark.parametrize(
        "question",
        [
            "List every customer's email address.",
            "What is the email of the customer named Smith?",
            "Group customers by their EMAIL and count them.",
            "show me emails",
        ],
    )
    def test_refuses_questions_naming_a_high_pii_column(self, question: str) -> None:
        reason = restricted_request(question, CATALOG)
        assert reason is not None
        assert "Customer.Email" in reason

    @pytest.mark.parametrize(
        "question",
        [
            "What is the total revenue across all invoices?",
            "How many customers are in the USA?",
            "What are the top 5 countries by revenue?",
            "List the customers based in Brazil.",
            "How many invoices were there each year?",
        ],
    )
    def test_allows_ordinary_questions(self, question: str) -> None:
        assert restricted_request(question, CATALOG) is None

    def test_runs_before_the_model_sees_anything(self) -> None:
        """The guard must not depend on the catalog being sent to an LLM first."""
        assert restricted_request("emails", CATALOG) is not None


class TestDroppedConcepts:
    def _revenue_by_billing_country(self):
        return (
            LQP.aggregate(table="Invoice")
            .group_by_columns(["Invoice.BillingCountry"])
            .agg("sum", "Invoice.Total", alias="revenue")
            .limit(24)
            .build()
        )

    def test_billing_country_plan_satisfies_both_country_readings(self) -> None:
        """'country' matches Customer.Country and Invoice.BillingCountry; either is a valid read."""
        plan = self._revenue_by_billing_country()
        question = "Show total invoice revenue for every billing country."
        assert dropped_concepts(question, plan, CATALOG) is None

    def test_generic_words_are_not_treated_as_requested_columns(self) -> None:
        """'in total' names Invoice.Total without asking for it."""
        plan = LQP.aggregate(table="Invoice").agg("count", None, alias="n").limit(1).build()
        assert dropped_concepts("How many invoices are there in total?", plan, CATALOG) is None

    def test_flags_a_plan_that_ignores_a_named_column(self) -> None:
        plan = LQP.aggregate(table="Invoice").agg("count", None, alias="n").limit(1).build()
        reason = dropped_concepts("How many invoices were billed to each country?", plan, CATALOG)
        assert reason is not None
        assert "Country" in reason

    def test_table_level_terms_are_too_weak_to_refuse_on(self) -> None:
        """'customer' could mean the table or CustomerId; do not refuse on that."""
        plan = LQP.aggregate(table="Customer").agg("count", None, alias="n").limit(1).build()
        assert dropped_concepts("How many customers do we have?", plan, CATALOG) is None

    def test_list_projection_counts_as_using_a_column(self) -> None:
        plan = (
            LQP.filter_and_list(table="Customer")
            .filter("Customer.Country", "eq", "Brazil")
            .limit(10)
            .build()
        )
        assert dropped_concepts("List the first name of customers in Brazil.", plan, CATALOG) is None

    def test_unknown_words_are_ignored(self) -> None:
        plan = LQP.aggregate(table="Invoice").agg("sum", "Invoice.Total", alias="r").limit(1).build()
        assert dropped_concepts("What is our total revenue, roughly?", plan, CATALOG) is None


class TestPlanConcepts:
    def test_collects_tables_columns_and_projection(self) -> None:
        plan = (
            LQP.aggregate(table="Invoice")
            .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
            .group_by_columns(["Customer.Country"])
            .agg("sum", "Invoice.Total", alias="revenue")
            .limit(10)
            .build()
        )
        concepts = plan_concepts(plan, CATALOG)
        assert Concept("Invoice") in concepts
        assert Concept("Customer") in concepts
        assert Concept("Customer", "Country") in concepts
        assert Concept("Invoice", "Total") in concepts
        assert Concept("Invoice", "CustomerId") in concepts


def test_average_question_answered_without_avg_is_refused() -> None:
    from secure_query.kernel.builder import LQP
    from secure_query.planner.clarify import code_from_guard_message
    from secure_query.planner.guard import dropped_average

    sum_and_count = (
        LQP.aggregate(table="Invoice")
        .agg("sum", "Invoice.Total", alias="total_revenue")
        .agg("count_distinct", "Invoice.CustomerId", alias="customers")
        .limit(1)
        .build()
    )
    msg = dropped_average("What is the mean spend per buyer?", sum_and_count)
    assert msg is not None
    assert code_from_guard_message(msg, refused=True) == "dropped_concept"

    with_avg = LQP.aggregate(table="Invoice").agg("avg", "Invoice.Total", alias="a").limit(1).build()
    assert dropped_average("What is the average invoice total?", with_avg) is None
    assert dropped_average("Total revenue by country", sum_and_count) is None


def test_calendar_words_are_not_out_of_scope() -> None:
    from secure_query.examples.sample_catalog import sample_catalog
    from secure_query.planner.guard import out_of_scope_request

    catalog = sample_catalog()
    for q in (
        "How many invoices were issued in the first half of 2023?",
        "Total revenue per quarter since January 2022",
        "Monthly invoice count before October 2024",
    ):
        assert out_of_scope_request(q, catalog) is None, q
    assert out_of_scope_request("What is our total payroll spend this quarter?", catalog) is not None


def test_dropped_literals_catches_missing_value_filter() -> None:
    from secure_query.kernel.logical_plan import LogicalPlan
    from secure_query.planner.guard import dropped_literals

    catalog = sample_catalog()
    base = {
        "plan_id": "00000000-0000-0000-0000-000000000001",
        "source": "Track",
        "group_by": {"columns": [{"table_id": "Album", "column_id": "Title"}]},
        "aggregations": [{"fn": "count", "column": None, "alias": "n"}],
        "limit": 1,
    }
    q = "Which AC/DC album has the most tracks?"
    assert "AC" in (dropped_literals(q, LogicalPlan.model_validate(base), catalog) or "")
    with_filter = {
        **base,
        "filters": [
            {"op": "eq", "column": {"table_id": "Artist", "column_id": "Name"},
             "value": {"type": "string", "value": "AC/DC"}}
        ],
    }
    assert dropped_literals(q, LogicalPlan.model_validate(with_filter), catalog) is None
    year_q = "How many invoices were there in 2023?"
    assert dropped_literals(year_q, LogicalPlan.model_validate(base), catalog) is not None


def test_dropped_literals_skips_sentence_starts_and_flags_invented_values() -> None:
    from secure_query.kernel.logical_plan import LogicalPlan
    from secure_query.planner.guard import dropped_literals

    catalog = sample_catalog()
    plan = {
        "plan_id": "00000000-0000-0000-0000-000000000001",
        "source": "Customer",
        "group_by": {"columns": [{"table_id": "Employee", "column_id": "LastName"}]},
        "aggregations": [{"fn": "count", "column": None, "alias": "n"}],
        "limit": 10,
    }
    q = "How many customers does each support rep look after? Show the rep's last name."
    assert dropped_literals(q, LogicalPlan.model_validate(plan), catalog) is None
    invented = {
        **plan,
        "filters": [
            {"op": "in", "column": {"table_id": "Customer", "column_id": "Country"},
             "values": [{"type": "string", "value": "Mexico"}]}
        ],
    }
    assert "Mexico" in (dropped_literals(q, LogicalPlan.model_validate(invented), catalog) or "")


def test_inexpressible_request() -> None:
    from secure_query.planner.guard import inexpressible_request

    assert inexpressible_request("How did revenue change month over month in 2024?")
    assert inexpressible_request("Which customers spent more than the average customer?")
    assert inexpressible_request("What is the average invoice total per country?") is None


def test_invented_number_threshold_is_flagged() -> None:
    from secure_query.kernel.logical_plan import LogicalPlan
    from secure_query.planner.guard import dropped_literals

    plan = LogicalPlan.model_validate(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "source": "Invoice",
            "group_by": {"columns": [{"table_id": "Invoice", "column_id": "BillingCountry"}]},
            "aggregations": [{"fn": "count", "column": None, "alias": "n"}],
            "having": [{"alias": "n", "op": "gt", "value": {"type": "float", "value": 20.0}}],
            "limit": 100,
        }
    )
    catalog = sample_catalog()
    assert dropped_literals("Which countries have more than 20 invoices?", plan, catalog) is None
    assert dropped_literals("Which countries have many invoices?", plan, catalog) is not None


def test_unrelated_metric() -> None:
    from secure_query.planner.guard import unrelated_metric

    assert unrelated_metric("How many tracks are in the catalog?", "line_item_revenue")
    assert unrelated_metric("How many employees work for us?", "employee_count") is None
    assert unrelated_metric("What is the average revenue per customer?", "avg_revenue_per_customer") is None
