"""Ask pipeline: confirm gate, SQL rejection, production auth."""

from __future__ import annotations

import pytest

from secure_query.api.service import ask, production_auth_blocked
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.planner import MockLLMClient
from tests.conftest import requires_chinook


def test_review_only_does_not_need_the_database() -> None:
    outcome = ask(
        "revenue by country",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        review_only=True,
    )
    assert outcome.status == "confirm"
    assert outcome.sql
    assert "SUM" in outcome.sql.upper()
    assert outcome.rows == []


def test_empty_question_is_invalid() -> None:
    outcome = ask(
        "  ",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        review_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "invalid_request"
    assert outcome.suggestions


def test_production_blocks_dev_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_ENV", "production")
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    assert production_auth_blocked() is not None
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    assert production_auth_blocked() is None


def test_sql_planner_mode_applies_row_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue
    from secure_query.planner import MockLLMClient

    usa_only = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    outcome = ask(
        "How many invoices are there?",
        principal=Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,)),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Invoice"})]),
        review_only=True,
    )
    assert outcome.status == "confirm"
    assert "EXISTS" in outcome.sql and "'USA'" in outcome.sql


@requires_chinook
def test_reviewed_sql_gets_row_filters_from_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The review carries the model's SQL before row filters; execute re-applies them,
    so a client cannot drop the principal's filter and keep the hash."""
    import json

    from secure_query.api.service import execute_reviewed
    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa_only = Eq(
        column=ColumnRef(table_id="Invoice", column_id="BillingCountry"),
        value=LiteralValue(type="string", value="USA"),
    )
    principal = Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,))
    reviewed = ask(
        "How many invoices are there?",
        principal=principal,
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) AS n FROM Invoice"})]),
        review_only=True,
    )
    assert reviewed.status == "confirm", reviewed.clarify_message
    assert "USA" not in reviewed.review["sql"]  # the model's SQL, before the filter
    assert "'USA'" in reviewed.sql
    ran = execute_reviewed(
        "How many invoices are there?", principal=principal, config=runtime_config(),
        catalog=sample_catalog(), **{k: v for k, v in reviewed.review.items() if k != "signature"},
        signature=reviewed.review["signature"],
    )
    assert ran.status == "ok" and ran.audit["plan_hash"] == reviewed.review["plan_hash"]
    assert "'USA'" in ran.sql


@requires_chinook
def test_ask_runs_the_consistency_gate_with_the_clients_own_second_reader(monkeypatch) -> None:
    """Regression: API and CLI pass their client in, and that used to switch the gate off."""
    import json

    monkeypatch.setenv("SECURE_QUERY_BACKEND", "duckdb")
    config = runtime_config()
    alice = Principal(principal_id="alice", tenant_id="chinook")

    def client(second_sql: str) -> MockLLMClient:
        first = MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Customer"})])
        first.check_twin = lambda: MockLLMClient([json.dumps({"sql": second_sql})])
        return first

    agree = ask("How many customers?", principal=alice, config=config, catalog=config.catalog,
                client=client("SELECT COUNT(CustomerId) FROM Customer"), review_only=True)
    assert agree.status == "confirm" and len(agree.evidence["gate_fingerprints"]) == 1
    differ = ask("How many customers?", principal=alice, config=config, catalog=config.catalog,
                 client=client("SELECT COUNT(*) FROM Employee"), review_only=True)
    assert differ.status == "clarify" and differ.clarify_code == "unstable_answer"


def test_real_client_supplies_a_sampling_twin_on_the_same_connection() -> None:
    pytest.importorskip("openai")
    from secure_query.planner.llm import CHECK_TEMPERATURE, OpenAIClient

    client = OpenAIClient(api_key="sk-test", provider="openai", model="any-model")
    twin = client.check_twin()
    assert twin is client.check_twin() and twin is not client
    assert twin.temperature == CHECK_TEMPERATURE and client.temperature == 0.0
    assert twin._model == "any-model" and twin._client is client._client


def test_second_reader_can_be_another_model(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    from secure_query.planner.llm import OpenAIClient

    monkeypatch.setenv("SECURE_QUERY_CHECK_MODEL", "other-model")
    client = OpenAIClient(api_key="sk-test", provider="openai", model="any-model")
    assert client.check_twin()._model == "other-model" and client._model == "any-model"
