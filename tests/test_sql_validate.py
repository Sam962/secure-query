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
    assert "LIMIT 1000" in compiled.compiled.sql
    assert compiled.compiled.plan_hash.startswith("sql:")
    assert ("Genre", "Name") in compiled.columns
    capped = validate_sql("SELECT Name FROM Genre LIMIT 999999", CATALOG)
    assert "LIMIT 1000" in capped.compiled.sql


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


def test_untokenizable_sql_is_a_validation_error() -> None:
    assert "sql.parse" in codes("SELECT Name FROM Genre WHERE Name = 'unterminated")


def test_fan_out_aggregates_are_rejected() -> None:
    inflated = (
        "SELECT SUM(i.Total) FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId"
    )
    assert "sql.fan_out" in codes(inflated)
    validate_sql(inflated.replace("SUM(i.Total)", "COUNT(DISTINCT i.InvoiceId)"), CATALOG)
    validate_sql(inflated.replace("SUM(i.Total)", "MAX(i.Total)"), CATALOG)
    # Many-side measure grouped by a one-side label: exact.
    validate_sql(
        "SELECT c.Country, SUM(i.Total) FROM Invoice i JOIN Customer c "
        "ON i.CustomerId = c.CustomerId GROUP BY c.Country",
        CATALOG,
    )


def test_identifiers_keep_catalog_case() -> None:
    sql = validate_sql("select name from genre", CATALOG).compiled.sql
    assert '"Genre"' in sql and '"Name"' in sql


ROW_FILTER_PROBES = [
    "SELECT COUNT(*) FROM Customer",
    "SELECT COUNT(*) FROM Customer AS c WHERE 1 = 1",
    "SELECT COUNT(*) FROM (SELECT CustomerId FROM Customer) t",
    "WITH all_c AS (SELECT CustomerId FROM Customer) SELECT COUNT(*) FROM all_c",
    "SELECT COUNT(*) FROM (SELECT CustomerId FROM Customer UNION ALL SELECT CustomerId FROM Customer) t",
]

USA_INVOICES = "SELECT COUNT(*) FROM Invoice i JOIN Customer c ON i.CustomerId = c.CustomerId WHERE c.Country = 'USA'"
INDIRECT_PROBES = [
    # Never mentions Customer: the filter must still reach Invoice through its join path.
    "SELECT COUNT(*) FROM Invoice",
    "SELECT COUNT(*) FROM (SELECT InvoiceId FROM Invoice) t",
    "SELECT COUNT(*) FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId "
    "WHERE il.InvoiceLineId IS NULL OR TRUE",
]


@pytest.mark.parametrize("sql", ROW_FILTER_PROBES)
def test_row_filters_cannot_be_escaped(sql: str) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH
    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    import duckdb

    usa = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    filtered = validate_sql(sql, CATALOG, row_filters=[usa]).compiled.sql
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        allowed = con.execute("SELECT COUNT(*) FROM Customer WHERE Country = 'USA'").fetchone()[0]
        got = con.execute(filtered).fetchone()[0]
    finally:
        con.close()
    assert got in (allowed, 2 * allowed)  # UNION ALL doubles the filtered rows, never more


@pytest.mark.parametrize("sql", INDIRECT_PROBES)
def test_row_filters_reach_related_tables(sql: str) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH
    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    import duckdb

    usa = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    filtered = validate_sql(sql, CATALOG, row_filters=[usa]).compiled.sql
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        got = con.execute(filtered).fetchone()[0]
        expected = con.execute(
            USA_INVOICES.replace(
                "FROM Invoice i", "FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId"
            )
            if "InvoiceLine" in sql
            else USA_INVOICES
        ).fetchone()[0]
    finally:
        con.close()
    assert got == expected


def test_row_filter_on_unrelated_table_is_rejected() -> None:
    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    cut = CATALOG.model_copy(
        update={"join_keys": [jk for jk in CATALOG.join_keys if jk.left_table != "PlaylistTrack"]}
    )
    with pytest.raises(SqlValidationFailed) as info:
        validate_sql("SELECT Name FROM Playlist", cut, row_filters=[usa])
    assert "sql.row_filter_unreachable" in {e.code for e in info.value.errors}


def test_sql_path_runs_semantic_guards_and_row_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from secure_query.planner import MockLLMClient
    from secure_query.planner.sql_plan import plan_sql_question

    dropped = plan_sql_question(
        "How many customers live in Brazil?",
        CATALOG,
        MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Customer"})]),
    )
    assert dropped.clarify_code == "dropped_filter" and dropped.blocked is not None

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    filtered = plan_sql_question(
        "How many invoices are there?",
        CATALOG,
        MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Invoice"})]),
        row_filters=[usa],
    )
    assert filtered.status == "ok" and "EXISTS" in filtered.compiled.sql


def test_fan_out_through_a_cte_is_still_checked() -> None:
    """A pass-through CTE has its base table's grain: the bypass is closed."""
    bypass = (
        "WITH i AS (SELECT InvoiceId, CustomerId, Total FROM Invoice) "
        "SELECT c.Country, SUM(c.SupportRepId) FROM Customer c JOIN i ON i.CustomerId = c.CustomerId "
        "GROUP BY c.Country"
    )
    assert "sql.fan_out" in codes(bypass)
    derived = (
        "SELECT c.Country, SUM(c.SupportRepId) FROM Customer c "
        "JOIN (SELECT CustomerId FROM Invoice) i ON i.CustomerId = c.CustomerId GROUP BY c.Country"
    )
    assert "sql.fan_out" in codes(derived)
    # Many-side measure through a pass-through CTE: exact, allowed.
    validate_sql(
        "WITH i AS (SELECT InvoiceId, CustomerId, Total FROM Invoice) SELECT c.Country, SUM(i.Total) "
        "FROM i JOIN Customer c ON i.CustomerId = c.CustomerId GROUP BY c.Country",
        CATALOG,
    )


def test_pre_aggregated_subquery_joined_on_its_key_is_allowed() -> None:
    validate_sql(
        "SELECT c.Country, SUM(t.spend) FROM Customer c JOIN "
        "(SELECT CustomerId, SUM(Total) AS spend FROM Invoice GROUP BY CustomerId) t "
        "ON t.CustomerId = c.CustomerId GROUP BY c.Country",
        CATALOG,
    )


def test_derived_side_of_unknown_grain_is_refused() -> None:
    assert "sql.fan_out_unknown_grain" in codes(
        "SELECT c.Country, SUM(c.SupportRepId) FROM Customer c JOIN "
        "(SELECT DISTINCT CustomerId FROM Invoice) i ON i.CustomerId = c.CustomerId GROUP BY c.Country"
    )


def test_pass_through_cte_limit_is_pushed_in() -> None:
    """WITH i AS (SELECT …) SELECT * FROM i LIMIT 10 must not scan the whole table."""
    import sqlglot

    compiled = validate_sql(
        "WITH i AS (SELECT InvoiceId, Total FROM Invoice) SELECT * FROM i LIMIT 10",
        CATALOG,
    ).compiled.sql
    tree = sqlglot.parse_one(compiled, read="duckdb")
    cte = (tree.ctes or [])[0].this
    assert cte.args.get("limit") is not None
    assert int(cte.args["limit"].expression.this) == 10


def test_joined_cte_is_not_limited() -> None:
    """A CTE used in a join still needs all its rows; do not push the outer LIMIT."""
    import sqlglot

    out = validate_sql(
        "WITH i AS (SELECT InvoiceId, CustomerId, Total FROM Invoice) "
        "SELECT c.Country, SUM(i.Total) AS revenue FROM i "
        "JOIN Customer c ON i.CustomerId = c.CustomerId GROUP BY c.Country LIMIT 10",
        CATALOG,
    ).compiled.sql
    cte = (sqlglot.parse_one(out, read="duckdb").ctes or [])[0].this
    assert cte.args.get("limit") is None


def test_select_star_cte_bypass_is_grain_checked_without_pii() -> None:
    """The review's probe, on a catalog where SELECT * is not already stopped by PII."""
    no_pii = CATALOG.model_copy(
        update={
            "tables": [
                t.model_copy(update={"columns": [c.model_copy(update={"pii_risk": "none"}) for c in t.columns]})
                for t in CATALOG.tables
            ]
        }
    )
    with pytest.raises(SqlValidationFailed) as info:
        validate_sql(
            "WITH i AS (SELECT * FROM Invoice) SELECT c.Country, SUM(c.SupportRepId) "
            "FROM Customer c JOIN i ON i.CustomerId = c.CustomerId GROUP BY c.Country",
            no_pii,
        )
    assert {e.code for e in info.value.errors} == {"sql.fan_out"}


def test_sql_is_prompted_and_parsed_in_the_catalog_dialect() -> None:
    """M8: a Postgres catalog is prompted for PostgreSQL and parsed as postgres."""
    from secure_query.planner.sql_plan import sql_system_prompt

    pg = CATALOG.model_copy(update={"sql_dialect": "postgres"})
    assert "PostgreSQL" in sql_system_prompt("postgres") and "DuckDB" not in sql_system_prompt("postgres")
    validate_sql("SELECT Name FROM Genre WHERE Name ILIKE 'r%'", pg)  # postgres syntax parses
    with pytest.raises(SqlValidationFailed):
        validate_sql("SELECT strptime(Name, '%Y') FROM Genre", pg)  # duckdb-only function
