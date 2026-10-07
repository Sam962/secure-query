"""HTTP API identity is server-side, never taken from the JSON body."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from secure_query.api import app
from secure_query.planner import MockLLMClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPAL_ID", "demo-user")
    monkeypatch.setenv("SECURE_QUERY_TENANT_ID", "chinook")
    monkeypatch.delenv("SECURE_QUERY_ALLOWED_TABLES", raising=False)
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    return TestClient(app)


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["backend"] == "duckdb"


def test_ready_and_ui(client: TestClient) -> None:
    ready = client.get("/ready")
    assert ready.status_code in {200, 503}
    page = client.get("/")
    assert page.status_code == 200
    assert b"Secure Query" in page.content
    assert b"Review plan" in page.content
    assert b"the model never writes SQL" in page.content


def test_ask_confirm_endpoint(client: TestClient) -> None:
    response = client.post("/ask/confirm", json={"question": "revenue by country"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"confirm", "clarify"}
    assert body["audit"]["principal_id"] == "demo-user"


def test_ask_rejects_forged_principal_fields(client: TestClient) -> None:
    response = client.post(
        "/ask",
        json={
            "question": "revenue by country",
            "principal_id": "admin",
            "tenant_id": "other-tenant",
            "allowed_tables": ["Employee"],
            "confirm_only": True,
        },
    )
    assert response.status_code == 422


def test_ask_confirm_uses_server_identity(client: TestClient) -> None:
    response = client.post(
        "/ask",
        json={"question": "revenue by country", "confirm_only": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["principal_id"] == "demo-user"
    assert body["audit"]["tenant_id"] == "chinook"
    assert body["status"] in {"confirm", "ok", "clarify"}
    assert "clarify_code" in body
    assert "suggestions" in body


def test_token_mode_rejects_missing_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv(
        "SECURE_QUERY_API_TOKENS",
        json.dumps({"secret": {"principal_id": "alice", "allowed_tables": ["Invoice"]}}),
    )
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    response = TestClient(app).post("/ask", json={"question": "hi", "confirm_only": True})
    assert response.status_code == 401


def test_health_reports_databricks_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    body = TestClient(app).get("/health").json()
    assert body["backend"] == "databricks"


def test_token_mode_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv(
        "SECURE_QUERY_API_TOKENS",
        json.dumps(
            {
                "secret": {
                    "principal_id": "alice",
                    "tenant_id": "chinook",
                    "allowed_tables": ["Invoice", "Customer"],
                }
            }
        ),
    )
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    response = TestClient(app).post(
        "/ask",
        headers={"Authorization": "Bearer secret"},
        json={"question": "revenue by country", "confirm_only": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["principal_id"] == "alice"
    assert body["status"] in {"confirm", "ok", "clarify"}


def _plan_json(group_column: str) -> str:
    return json.dumps(
        {
            "source": "Invoice",
            "filters": [],
            "group_by": {"columns": [{"table_id": "Invoice", "column_id": group_column}], "time_buckets": []},
            "aggregations": [{"fn": "count", "column": None, "alias": "invoice_count"}],
            "having": [],
            "order_by": [{"alias": "invoice_count", "direction": "desc"}],
            "limit": 100,
        }
    )


class _NoPlanner:
    """Run must not re-plan: any LLM call fails the test (and would return plan B)."""

    provider = "mock"

    def complete(self, messages):
        raise AssertionError("/ask/execute called the planner")


def test_run_executes_the_reviewed_plan_not_a_replan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH

    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    question = "How many invoices per billing country?"
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([_plan_json("BillingCountry")]),
    )
    reviewed = client.post("/ask/confirm", json={"question": question}).json()
    assert reviewed["status"] == "confirm", reviewed
    review = reviewed["review"]
    assert review["plan_hash"] == reviewed["audit"]["plan_hash"]

    # A second planner call would now produce a different plan (BillingCity) —
    # and any planner call at all fails the test.
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([_plan_json("BillingCity")]),
    )
    monkeypatch.setattr("secure_query.api.service.plan_question", _NoPlanner().complete)
    monkeypatch.setattr("secure_query.api.service.plan_sql_question", _NoPlanner().complete)
    ran = client.post("/ask/execute", json={**review, "question": question})
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["status"] == "ok"
    assert body["audit"]["plan_hash"] == review["plan_hash"]
    assert body["sql"] == reviewed["sql"]
    assert "BillingCity" not in body["sql"]


def test_execute_rejects_a_plan_that_differs_from_the_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([_plan_json("BillingCountry")]),
    )
    review = client.post(
        "/ask/confirm", json={"question": "How many invoices per billing country?"}
    ).json()["review"]
    tampered = json.loads(_plan_json("BillingCity"))
    tampered.update(plan_id=review["plan"]["plan_id"], schema_version="lqp/1")
    response = client.post("/ask/execute", json={**review, "plan": tampered})
    assert response.status_code == 409


def test_sql_path_run_reuses_the_reviewed_sql(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH

    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    monkeypatch.setenv("SECURE_QUERY_PLANNER", "sql")
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) AS n FROM Invoice"})]),
    )
    reviewed = client.post("/ask/confirm", json={"question": "How many invoices are there?"}).json()
    review = reviewed["review"]
    assert review["sql"] and review["plan"] is None
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: _NoPlanner())
    body = client.post("/ask/execute", json={**review, "question": "How many invoices are there?"}).json()
    assert body["status"] == "ok" and body["audit"]["plan_hash"] == review["plan_hash"]


def test_two_asks_share_one_llm_client_and_one_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.engine import runtime as runtime_module
    from secure_query.planner import llm

    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_TENANT_ID", "chinook")
    built = {"client": 0, "runtime": 0}
    real_runtime = runtime_module.runtime_config

    def count_client():
        built["client"] += 1
        return MockLLMClient()

    def count_runtime():
        built["runtime"] += 1
        return real_runtime()

    monkeypatch.setattr(llm, "default_client", count_client)
    monkeypatch.setattr(runtime_module, "runtime_config", count_runtime)
    client = TestClient(app)
    for _ in range(2):
        assert client.post("/ask/confirm", json={"question": "revenue by country"}).status_code == 200
    assert built == {"client": 1, "runtime": 1}
    assert runtime_module.get_runtime() is runtime_module.get_runtime()
