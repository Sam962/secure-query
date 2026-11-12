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


def test_like_ignores_case() -> None:
    from secure_query.planner.sql_values import case_insensitive_like

    out = case_insensitive_like("SELECT Name FROM Genre WHERE Name LIKE '%rock%'", CATALOG)
    assert "ILIKE '%rock%'" in out
    plain = "SELECT Name FROM Genre WHERE Name = 'Rock'"
    assert case_insensitive_like(plain, CATALOG) == plain


def test_unmatched_literals_names_values_no_row_has() -> None:
    from secure_query.planner.sql_values import unmatched_literals

    sql = "SELECT Name FROM Genre WHERE Name IN ('Rock', 'Polka')"
    assert unmatched_literals(sql, CATALOG, probe_returning("Rock", "Rock And Roll")) == [("Genre", "Name", "Polka")]


class RecordingClient:
    """Returns canned SQL responses and keeps every message the planner sent."""

    def __init__(self, *sql: str) -> None:
        self.responses = list(sql)
        self.sent: list[str] = []

    def complete(self, messages, response_format=None) -> str:
        import json

        self.sent += [m["content"] for m in messages]
        return json.dumps({"sql": self.responses.pop(0)})


def _plan(question: str, client: RecordingClient, *values: str):
    from secure_query.planner.sql_plan import plan_sql_question

    return plan_sql_question(question, CATALOG, client, value_probe=probe_returning(*values))


def test_missing_value_spends_the_repair_round_without_revealing_values() -> None:
    sql = "SELECT COUNT(*) AS n FROM Genre WHERE Name = 'Polka'"
    client = RecordingClient(sql, sql)
    result = _plan("How many genres are called Polka?", client, "Pop", "Rock")
    assert result.status == "ok" and result.attempts == 2  # kept: an absent value is a valid answer
    feedback = client.sent[-1]
    assert "no stored Genre.Name equals 'Polka'" in feedback
    assert "Rock" not in feedback and "'Pop'" not in feedback


def test_corrected_value_after_feedback_is_used() -> None:
    client = RecordingClient(
        "SELECT COUNT(*) AS n FROM Genre WHERE Name = 'Pop music'",
        "SELECT COUNT(*) AS n FROM Genre WHERE Name = 'Pop'",
    )
    result = _plan("How many pop genres are there?", client, "Pop")
    assert result.status == "ok" and "'Pop'" in result.compiled.sql


def test_ranked_answer_shows_the_number_it_was_ranked_by() -> None:
    from secure_query.kernel.sql_validate import validate_sql
    from secure_query.planner.sql_values import show_ranking_measure

    def shown(sql: str) -> str:
        return validate_sql(show_ranking_measure(sql, CATALOG), CATALOG).compiled.sql

    top = shown(
        "SELECT ar.Name FROM Album al JOIN Artist ar ON al.ArtistId = ar.ArtistId "
        "GROUP BY ar.Name ORDER BY COUNT(*) DESC LIMIT 1"
    )
    assert 'COUNT(*) AS "count"' in top
    over = shown(
        "SELECT c.Country FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId "
        "GROUP BY c.Country HAVING SUM(i.Total) > 40"
    )
    assert 'SUM("i"."Total") AS "sum_total"' in over
    ratio = shown(
        "SELECT g.Name FROM Track t JOIN Genre g ON t.GenreId = g.GenreId "
        "GROUP BY g.Name ORDER BY SUM(t.Milliseconds) / COUNT(*) DESC LIMIT 3"
    )
    assert 'AS "ranked_by_milliseconds"' in ratio and ratio.count(" AS ") == 4  # one new column, not two


def test_ranking_measure_leaves_other_queries_alone() -> None:
    from secure_query.planner.sql_values import show_ranking_measure

    for sql in (
        "SELECT BillingCountry, COUNT(*) FROM Invoice GROUP BY BillingCountry ORDER BY COUNT(*) DESC LIMIT 2",
        "SELECT BillingCountry, COUNT(*) AS n FROM Invoice GROUP BY BillingCountry ORDER BY n DESC",
        "SELECT Name FROM Track ORDER BY Milliseconds DESC LIMIT 1",  # not grouped: no aggregate to show
        "SELECT DISTINCT BillingCountry FROM Invoice",
        "SELECT COUNT(*) FROM Invoice",
    ):
        assert show_ranking_measure(sql, CATALOG) == sql
