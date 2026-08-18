"""Tests for catalog synonyms and out-of-scope guards."""

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.guard import catalog_vocabulary, opaque_grouping_keys, out_of_scope_request
from secure_query.builder import LQP


def test_revenue_synonym_maps_to_invoice_total() -> None:
    catalog = sample_catalog()
    vocab = catalog_vocabulary(catalog)
    assert "revenue" in vocab
    targets = {str(c) for c in vocab["revenue"]}
    assert "Invoice.Total" in targets


def test_genre_synonym_in_vocabulary() -> None:
    catalog = sample_catalog()
    vocab = catalog_vocabulary(catalog)
    assert "genre" in vocab


def test_supplier_question_is_out_of_scope() -> None:
    catalog = sample_catalog()
    msg = out_of_scope_request(
        "How much did we spend with our suppliers last quarter?", catalog
    )
    assert msg is not None
    assert "supplier" in msg.lower()


def test_brazil_customer_question_not_out_of_scope() -> None:
    catalog = sample_catalog()
    assert (
        out_of_scope_request("How many customers are located in Brazil?", catalog) is None
    )


def test_countries_plural_not_out_of_scope() -> None:
    catalog = sample_catalog()
    assert (
        out_of_scope_request(
            "What are the top 5 customer countries by total invoice revenue?", catalog
        )
        is None
    )


def test_year_literal_not_out_of_scope() -> None:
    catalog = sample_catalog()
    assert out_of_scope_request("How many invoices were issued in 2010?", catalog) is None


def test_opaque_grouping_refuses_artist_id() -> None:
    catalog = sample_catalog()
    plan = (
        LQP.aggregate(table="Album")
        .group_by_columns(["Album.ArtistId"])
        .agg("count", None, alias="album_count")
        .limit(1)
        .build()
    )
    reason = opaque_grouping_keys(
        "Which artist has the most albums? Just the top one.", plan, catalog
    )
    assert reason is not None
    assert "Artist.Name" in reason


def test_revenue_question_not_out_of_scope() -> None:
    catalog = sample_catalog()
    assert out_of_scope_request("What is the total revenue across all invoices?", catalog) is None


def test_employee_headcount_question_not_out_of_scope() -> None:
    catalog = sample_catalog()
    assert (
        out_of_scope_request("How many employees work for us?", catalog) is None
    )


def test_employee_synonyms_in_vocabulary() -> None:
    catalog = sample_catalog()
    vocab = catalog_vocabulary(catalog)
    for term in ("employee", "employees", "headcount"):
        assert term in vocab
        assert any(c.table == "Employee" and c.column is None for c in vocab[term])
