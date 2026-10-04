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
    useless_joins,
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


class TestUselessJoins:
    def test_flags_a_join_whose_table_is_never_read(self) -> None:
        """Album JOIN Artist with no grouping returns one grand total, not per artist."""
        plan = (
            LQP.aggregate(table="Album")
            .join("Artist", on=[("Album.ArtistId", "Artist.ArtistId")])
            .agg("count", None, alias="album_count")
            .limit(1)
            .build()
        )
        reason = useless_joins(plan)
        assert reason is not None
        assert "Artist" in reason

    def test_allows_a_join_used_for_grouping(self) -> None:
        plan = (
            LQP.aggregate(table="Album")
            .join("Artist", on=[("Album.ArtistId", "Artist.ArtistId")])
            .group_by_columns(["Artist.Name"])
            .agg("count", None, alias="album_count")
            .limit(10)
            .build()
        )
        assert useless_joins(plan) is None

    def test_allows_a_bridge_table(self) -> None:
        """InvoiceLine -> Track -> Genre reads no Track column, but Track is the path."""
        plan = (
            LQP.aggregate(table="InvoiceLine")
            .join("Track", on=[("InvoiceLine.TrackId", "Track.TrackId")])
            .join("Genre", on=[("Track.GenreId", "Genre.GenreId")])
            .group_by_columns(["Genre.Name"])
            .agg("count", None, alias="sold")
            .limit(10)
            .build()
        )
        assert useless_joins(plan) is None

    def test_allows_a_join_used_only_for_filtering(self) -> None:
        plan = (
            LQP.aggregate(table="Invoice")
            .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
            .filter("Customer.Country", "eq", "USA")
            .agg("sum", "Invoice.Total", alias="revenue")
            .limit(1)
            .build()
        )
        assert useless_joins(plan) is None

    def test_no_joins_is_fine(self) -> None:
        plan = LQP.aggregate(table="Invoice").agg("count", None, alias="n").limit(1).build()
        assert useless_joins(plan) is None


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
