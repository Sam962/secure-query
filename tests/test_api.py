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
    response = client.post("/ask/review", json={"question": "revenue by country"})
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
            "review_only": True,
        },
    )
    assert response.status_code == 422


def test_ask_confirm_uses_server_identity(client: TestClient) -> None:
    response = client.post(
        "/ask",
        json={"question": "revenue by country", "review_only": True},
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
    response = TestClient(app).post("/ask", json={"question": "hi", "review_only": True})
    assert response.status_code == 401


def test_health_reports_databricks_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from secure_query.demo.chinook import sample_catalog

    catalog = tmp_path / "catalog.json"
    catalog.write_text(sample_catalog().model_copy(update={"tenant_id": "file"}).model_dump_json())
    monkeypatch.setenv("SECURE_QUERY_CATALOG_FILE", str(catalog))
    monkeypatch.setenv("SECURE_QUERY_BACKEND", "databricks")
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
        json={"question": "revenue by country", "review_only": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["principal_id"] == "alice"
    assert body["status"] in {"confirm", "ok", "clarify"}


def _sql_json(group_column: str) -> str:
    return json.dumps(
        {"sql": f'SELECT "{group_column}", COUNT(*) AS invoice_count FROM "Invoice" '
                f'GROUP BY "{group_column}" ORDER BY invoice_count DESC LIMIT 100'}
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
        lambda: MockLLMClient([_sql_json("BillingCountry")]),
    )
    reviewed = client.post("/ask/review", json={"question": question}).json()
    assert reviewed["status"] == "confirm", reviewed
    review = reviewed["review"]
    assert review["plan_hash"] == reviewed["audit"]["plan_hash"]

    # A second planner call would now produce a different plan (BillingCity) —
    # and any planner call at all fails the test.
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([_sql_json("BillingCity")]),
    )
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
        lambda: MockLLMClient([_sql_json("BillingCountry")]),
    )
    review = client.post(
        "/ask/review", json={"question": "How many invoices per billing country?"}
    ).json()["review"]
    tampered = json.loads(_sql_json("BillingCity"))["sql"]
    response = client.post("/ask/execute", json={**review, "sql": tampered})
    assert response.status_code == 403  # the signature covers the reviewed SQL


def test_execute_refuses_sql_the_server_did_not_review(client: TestClient) -> None:
    """Review probe: caller-written SQL with its own plan_hash must not run."""
    from secure_query.engine.runtime import get_runtime
    from secure_query.kernel.sql_validate import validate_sql

    sql = "SELECT BillingCity, SUM(Total) AS t FROM Invoice GROUP BY BillingCity ORDER BY t DESC LIMIT 3"
    plan_hash = validate_sql(sql, get_runtime().catalog).compiled.plan_hash
    for signature in ("", "0" * 64):
        response = client.post("/ask/execute", json={"plan_hash": plan_hash, "sql": sql, "signature": signature})
        assert response.status_code == 403


def test_review_is_bound_to_the_principal_it_was_issued_to(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.api.service import ReviewForbidden, execute_reviewed, sign_review
    from secure_query.auth import Principal
    from secure_query.engine.runtime import get_runtime

    alice = Principal(principal_id="alice", tenant_id="chinook")
    bob = Principal(principal_id="bob", tenant_id="chinook")
    review = {"plan_hash": "sql:x", "plan": None, "sql": "SELECT 1", "metric_id": None}
    config = get_runtime()
    with pytest.raises(ReviewForbidden):
        execute_reviewed(
            "q", principal=bob, config=config, catalog=config.catalog, signature=sign_review(review, alice),
            plan_hash="sql:x", sql="SELECT 1",
        )


def test_sql_path_run_reuses_the_reviewed_sql(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH

    if not DUCKDB_PATH.exists():
        pytest.skip("sample DB not built")
    monkeypatch.setattr(
        "secure_query.api.http.get_client",
        lambda: MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) AS n FROM Invoice"})]),
    )
    reviewed = client.post("/ask/review", json={"question": "How many invoices are there?"}).json()
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
        assert client.post("/ask/review", json={"question": "revenue by country"}).status_code == 200
    assert built == {"client": 1, "runtime": 1}
    assert runtime_module.get_runtime() is runtime_module.get_runtime()


def test_execute_errors_reach_the_client_without_database_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: the client gets a generic message and the audit reference, never DB detail."""
    from secure_query.engine.execute import ExecutionError

    def boom(*_a, **_k):
        raise ExecutionError(
            'Catalog Error: Table with name "SecretPayroll" does not exist at /srv/data/x.duckdb',
            audit_id="abc123",
        )

    monkeypatch.setattr("secure_query.api.http.ask", boom)
    response = client.post("/ask", json={"question": "anything"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "abc123" in detail
    assert "SecretPayroll" not in detail and "/srv" not in detail


def test_planner_failure_is_a_502_not_a_traceback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secure_query.planner import PlannerError

    def down(*_a, **_k):
        raise PlannerError("LLM request failed (openai gpt): Error code: 401 - invalid_api_key")

    monkeypatch.setattr("secure_query.api.http.ask", down)
    response = client.post("/ask/review", json={"question": "anything"})
    assert response.status_code == 502
    assert "invalid_api_key" not in response.json()["detail"]


def test_ready_does_not_name_the_tenant(client: TestClient) -> None:
    body = client.get("/ready").json()
    assert "catalog_tenant" not in body and "chinook" not in json.dumps(body)


def test_ready_reports_the_databricks_grant_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H7: a warehouse principal with write privileges is not ready."""
    from secure_query.engine import runtime as runtime_module

    config = runtime_module.runtime_config()
    monkeypatch.setattr(
        "secure_query.api.http.get_runtime", lambda: config.__class__(**{**config.__dict__, "backend": "databricks"})
    )
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "shared")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    monkeypatch.setattr("secure_query.api.http.missing_catalog_tables", lambda tables: [])
    monkeypatch.setattr(
        "secure_query.api.http.databricks_grant_check",
        lambda: {"status": "select_only", "detail": "svc: SELECT"},
    )
    assert client.get("/ready").json()["databricks_grants"] == "select_only"
    monkeypatch.setattr(
        "secure_query.api.http.databricks_grant_check",
        lambda: {"status": "write_privileges", "detail": "svc has MODIFY on main.sales"},
    )
    response = client.get("/ready")
    assert response.status_code == 503 and "MODIFY" not in response.text


def test_ready_checks_the_catalog_against_the_warehouse(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """In user mode the service token's grants say nothing; the tables must still be there."""
    from secure_query.engine import runtime as runtime_module

    config = runtime_module.runtime_config()
    monkeypatch.setattr(
        "secure_query.api.http.get_runtime", lambda: config.__class__(**{**config.__dict__, "backend": "databricks"})
    )
    monkeypatch.setenv("SECURE_QUERY_DATABRICKS_AUTH", "user")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat")
    monkeypatch.setattr("secure_query.api.http.databricks_grant_check", lambda: 1 / 0)  # never called in user mode
    monkeypatch.setattr("secure_query.api.http.missing_catalog_tables", lambda tables: [])
    assert client.get("/ready").json()["databricks_grants"] == "per_user"
    monkeypatch.setattr("secure_query.api.http.missing_catalog_tables", lambda tables: ["Invoice"])
    response = client.get("/ready")
    assert response.status_code == 503 and "Invoice" not in response.text


def test_signed_review_expires_and_survives_key_rotation(monkeypatch) -> None:
    import time

    import pytest

    from secure_query.api.service import ReviewForbidden, sign_review, verify_review
    from secure_query.auth import Principal

    alice = Principal(principal_id="alice", tenant_id="t")
    review = {"plan_hash": "abc", "plan": None, "sql": "SELECT 1", "metric_id": None}
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "old-secret-old-secret-old-secret-123")
    signature = sign_review(review, alice)
    verify_review(review, alice, signature)

    expired = sign_review(review, alice, now=time.time() - 3600)
    with pytest.raises(ReviewForbidden, match="expired"):
        verify_review(review, alice, expired)
    later, _, digest = signature.partition(".")
    with pytest.raises(ReviewForbidden):  # the expiry is inside the signature
        verify_review(review, alice, f"{int(later) + 9999}.{digest}")

    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "new-secret-new-secret-new-secret-456")
    with pytest.raises(ReviewForbidden):
        verify_review(review, alice, signature)
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET_PREVIOUS", "old-secret-old-secret-old-secret-123")
    verify_review(review, alice, signature)
