from decimal import Decimal
from types import SimpleNamespace

from secure_query.api.respond import (
    format_result_cell,
    format_result_value,
    format_template_answer,
    money_scale_note,
    result_column_units,
)
from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.builder import LQP


def test_binary_float_dust_rounds_to_cents() -> None:
    assert format_result_value(523.0600000000003) == 523.06
    assert format_result_value(303.9599999999999) == 303.96
    assert format_result_cell(195.09999999999994) == "195.10"


def test_usd_cell_shows_dollar_sign() -> None:
    assert format_result_cell(523.0600000000003, unit="USD") == "$523.06"


def test_real_precision_is_not_forced_to_two_decimals() -> None:
    """A genuine ratio is not money-rounded just because it is a float."""
    value = 39.473684210526
    cleaned = format_result_value(value)
    assert isinstance(cleaned, float)
    assert abs(cleaned - value) < 1e-9
    assert format_result_cell(value) != "39.47"


def test_ints_and_decimals() -> None:
    assert format_result_value(21) == 21
    assert format_result_value(Decimal("46.62")) == 46.62
    assert format_result_value(None) is None
    assert format_result_value(True) is True


def test_template_table_uses_two_decimal_money() -> None:
    result = SimpleNamespace(
        columns=["Country", "revenue"],
        rows=[("USA", 523.0600000000003), ("Canada", 303.9599999999999)],
        truncated=False,
    )
    text = format_template_answer("revenue by country", None, result)  # type: ignore[arg-type]
    assert "523.06" in text
    assert "303.96" in text
    assert "0000003" not in text


def test_revenue_alias_inherits_usd_and_hundreds_band() -> None:
    catalog = sample_catalog()
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
        .group_by_columns(["Customer.Country"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(10)
        .build()
    )
    units = result_column_units(["Country", "revenue"], plan=plan, catalog=catalog)
    assert units == [None, "USD"]
    note = money_scale_note(
        ["Country", "revenue"],
        [("USA", 523.06), ("Canada", 303.96)],
        units,
    )
    assert note is not None
    assert "USD" in note
    assert "hundreds" in note
    assert "not in thousands or millions" in note

    result = SimpleNamespace(
        columns=["Country", "revenue"],
        rows=[("USA", 523.0600000000003)],
        truncated=False,
    )
    text = format_template_answer(
        "revenue by country",
        plan,
        result,  # type: ignore[arg-type]
        catalog=catalog,
    )
    assert "revenue (USD)" in text
    assert "$523.06" in text
