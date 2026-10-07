"""Grounding SQL string literals against stored values (fake probes, no database)."""

from __future__ import annotations

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue
from secure_query.planner.sql_values import ground_literals
from tests.conftest import requires_chinook

CATALOG = sample_catalog()


def probe_returning(*values: str, seen: list[str] | None = None):
    def probe(compiled):
        if seen is not None:
            seen.append(compiled.sql)
        return [(v,) for v in values]

    return probe


def test_misspelled_literal_is_replaced_by_the_one_stored_value() -> None:
    sql = "SELECT Name FROM Genre WHERE Name = 'rock and roll'"
    out = ground_literals(sql, CATALOG, probe_returning("Rock And Roll", "Rock"))
    assert "'Rock And Roll'" in out and "'rock and roll'" not in out


def test_spacing_and_punctuation_are_ignored() -> None:
    out = ground_literals(
        "SELECT Title FROM Album WHERE Title = 'Big Ones'", CATALOG, probe_returning("BigOnes!")
    )
    assert "'BigOnes!'" in out


def test_stored_literal_is_left_alone() -> None:
    sql = "SELECT Name FROM Genre WHERE Name = 'Rock'"
    assert ground_literals(sql, CATALOG, probe_returning("Rock", "Rock And Roll")) == sql


def test_two_stored_spellings_are_ambiguous() -> None:
    sql = "SELECT Name FROM Genre WHERE Name = 'rock'"
    assert ground_literals(sql, CATALOG, probe_returning("Rock", "ROCK")) == sql


def test_partial_matches_are_not_substituted() -> None:
    sql = "SELECT Name FROM Genre WHERE Name = 'Metal'"
    assert ground_literals(sql, CATALOG, probe_returning("Heavy Metal")) == sql


def test_in_lists_are_grounded() -> None:
    out = ground_literals(
        "SELECT Name FROM Genre WHERE Name IN ('jazz', 'Rock')", CATALOG, probe_returning("Jazz", "Rock")
    )
    assert "'Jazz'" in out and "'Rock'" in out


def test_failed_lookup_leaves_sql_as_written() -> None:
    def broken(_compiled):
        raise RuntimeError("warehouse down")

    sql = "SELECT Name FROM Genre WHERE Name = 'rock'"
    assert ground_literals(sql, CATALOG, broken) == sql


def test_lookup_applies_principal_row_filters() -> None:
    usa = Eq(
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    seen: list[str] = []
    ground_literals(
        "SELECT City FROM Customer WHERE City = 'boston'",
        CATALOG,
        probe_returning("Boston", seen=seen),
        row_filters=[usa],
    )
    assert seen and "'USA'" in seen[0]


def test_numeric_columns_are_not_probed() -> None:
    seen: list[str] = []
    ground_literals("SELECT Total FROM Invoice WHERE Total = 1", CATALOG, probe_returning(seen=seen))
    assert seen == []


@requires_chinook
def test_sql_planner_grounds_against_the_database() -> None:
    import json
    from functools import partial

    from secure_query.evals.accuracy import _probe
    from secure_query.planner import MockLLMClient
    from secure_query.planner.sql_plan import plan_sql_question
    from tests.conftest import CHINOOK_DB

    client = MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) AS n FROM Customer WHERE Country = 'usa'"})])
    result = plan_sql_question(
        "How many customers are in the usa?", CATALOG, client, value_probe=partial(_probe, CHINOOK_DB)
    )
    assert result.status == "ok" and "'USA'" in result.compiled.sql and "'USA'" in result.source_sql
