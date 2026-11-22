"""Gold SQL from SQLite keeps its meaning on Databricks."""

from secure_query.evals.warehouse import convert


def test_text_summed_as_numbers_and_like_ignores_case() -> None:
    sql = convert("SELECT c FROM t WHERE n LIKE 'a%' GROUP BY c ORDER BY sum(x) DESC LIMIT 1", {"c", "n", "x"})
    assert "SUM(TRY_CAST(x AS DOUBLE))" in sql and "ILIKE" in sql


def test_bare_grouped_column_becomes_any_value() -> None:
    assert "ANY_VALUE(b)" in convert("SELECT a, b FROM t GROUP BY a", {"a", "b"})
