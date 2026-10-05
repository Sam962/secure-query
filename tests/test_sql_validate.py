"""The SQL path's validator is the security boundary: adversarial cases first."""

from __future__ import annotations

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql

CATALOG = sample_catalog()


def codes(sql: str) -> set[str]:
    with pytest.raises(SqlValidationFailed) as info:
        validate_sql(sql, CATALOG)
    return {e.code for e in info.value.errors}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM Invoice",
        "DROP TABLE Invoice",
        "INSERT INTO Genre VALUES (99, 'x')",
        "UPDATE Invoice SET Total = 0",
        "ATTACH 'x.db' AS other",
        "COPY Invoice TO 'out.csv'",
        "PRAGMA database_list",
        "SET threads = 1",
    ],
)
def test_only_read_only_queries(sql: str) -> None:
    assert codes(sql) & {"sql.read_only", "sql.parse"}


def test_one_statement_only() -> None:
    assert "sql.statements" in codes("SELECT 1 FROM Genre; DROP TABLE Genre")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://bucket/x')",
        "SELECT * FROM other_db.main.Invoice",
        "SELECT * FROM secrets",
    ],
)
def test_sources_must_be_catalog_tables(sql: str) -> None:
    assert codes(sql) & {"sql.table_function", "sql.unknown_table"}


def test_unknown_column_is_rejected() -> None:
    assert "sql.unknown_column" in codes("SELECT Salary FROM Employee")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT Email FROM Customer",
        "SELECT * FROM Customer",
        "SELECT COUNT(*) FROM Customer WHERE Email LIKE '%gmail%'",
        "SELECT c.FirstName FROM Customer c ORDER BY c.Phone",
        "SELECT x FROM (SELECT Email AS x FROM Customer) t",
        "WITH e AS (SELECT Email FROM Customer) SELECT * FROM e",
        "SELECT Name FROM Artist UNION SELECT Email FROM Customer",
        "SELECT Total FROM Invoice WHERE CustomerId IN (SELECT CustomerId FROM Customer WHERE Phone = '1')",
        'select EMAIL from customer',
        'SELECT "Customer"."Email" FROM "Customer"',
        "SELECT /* harmless */ Email -- x\nFROM Customer",
        "SELECT Email AS Name FROM Customer",
        "SELECT i.Total FROM Invoice i WHERE EXISTS "
        "(SELECT 1 FROM Customer c WHERE c.CustomerId = i.CustomerId AND c.Fax IS NOT NULL)",
        "SELECT COUNT(DISTINCT Address) FROM Customer",
    ],
)
def test_pii_columns_are_unreachable(sql: str) -> None:
    assert "sql.pii" in codes(sql)


def test_unknown_functions_are_rejected() -> None:
    assert "sql.function" in codes("SELECT getenv('HOME') FROM Genre")


def test_joins_must_use_approved_keys() -> None:
    assert "sql.cross_join" in codes("SELECT * FROM Genre, MediaType")
    assert "sql.cross_join" in codes("SELECT * FROM Genre CROSS JOIN MediaType")
    assert "sql.join_not_allowed" in codes(
        "SELECT g.Name FROM Genre g JOIN MediaType m ON g.GenreId = m.MediaTypeId"
    )


def test_valid_query_is_regenerated_and_limited() -> None:
    compiled = validate_sql(
        "SELECT g.Name, COUNT(*) AS n FROM Track t JOIN Genre g ON t.GenreId = g.GenreId "
        "GROUP BY g.Name ORDER BY n DESC",
        CATALOG,
    )
    assert "LIMIT 1000" in compiled.sql
    assert compiled.plan_hash.startswith("sql:")
    capped = validate_sql("SELECT Name FROM Genre LIMIT 999999", CATALOG)
    assert "LIMIT 1000" in capped.sql


def test_nested_and_set_queries_are_expressible() -> None:
    validate_sql(
        "SELECT Name FROM Artist WHERE ArtistId NOT IN (SELECT ArtistId FROM Album)", CATALOG
    )
    validate_sql(
        "SELECT BillingCountry FROM Invoice WHERE Total > 20 "
        "INTERSECT SELECT BillingCountry FROM Invoice WHERE Total < 1",
        CATALOG,
    )


def test_sql_planner_validates_refuses_and_repairs() -> None:
    import json

    from secure_query.planner import MockLLMClient
    from secure_query.planner.sql_plan import plan_sql_question

    ok = plan_sql_question(
        "How many genres?",
        CATALOG,
        MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Genre"})]),
    )
    assert ok.status == "ok" and "LIMIT" in ok.compiled.sql

    refused = plan_sql_question(
        "Payroll?", CATALOG, MockLLMClient([json.dumps({"cannot_answer": True, "reason": "no"})])
    )
    assert refused.status == "clarify" and refused.refused_by_model

    client = MockLLMClient(
        [json.dumps({"sql": "SELECT Email FROM Customer"}), json.dumps({"sql": "SELECT COUNT(*) FROM Customer"})]
    )
    repaired = plan_sql_question("How many customers?", CATALOG, client)
    assert repaired.status == "ok" and repaired.attempts == 2
    assert "sql.pii" in client.calls[1][-1]["content"]  # the repair prompt names the violation


def test_unused_join_is_rejected_unless_deduplicated() -> None:
    unused = (
        "SELECT t.Name FROM Track t JOIN InvoiceLine il ON il.TrackId = t.TrackId"
    )
    assert "sql.unused_join" in codes(unused)
    validate_sql(unused.replace("SELECT t.Name", "SELECT DISTINCT t.Name"), CATALOG)
    validate_sql(
        "SELECT t.Name, il.UnitPrice FROM Track t JOIN InvoiceLine il ON il.TrackId = t.TrackId",
        CATALOG,
    )
