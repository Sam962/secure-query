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
    monkeypatch.setattr("secure_query.api.default_client", lambda: MockLLMClient())
    return TestClient(app)


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["backend"] == "duckdb"


def test_ask_does_not_honor_forged_principal_fields(client: TestClient) -> None:
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
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["principal_id"] == "demo-user"
    assert body["audit"]["tenant_id"] == "chinook"


def test_token_mode_rejects_missing_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv(
        "SECURE_QUERY_API_TOKENS",
        json.dumps({"secret": {"principal_id": "alice", "allowed_tables": ["Invoice"]}}),
    )
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.setattr("secure_query.api.default_client", lambda: MockLLMClient())
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
    monkeypatch.setattr("secure_query.api.default_client", lambda: MockLLMClient())
    response = TestClient(app).post(
        "/ask",
        headers={"Authorization": "Bearer secret"},
        json={"question": "revenue by country", "confirm_only": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["principal_id"] == "alice"
    assert body["status"] in {"confirm", "ok", "clarify"}
