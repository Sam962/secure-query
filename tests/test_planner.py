"""Phase 2 planner tests — mock LLM only (no network)."""

from __future__ import annotations

import json

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.planner import (
    SYSTEM_PROMPT,
    MockLLMClient,
    build_repair_prompt,
    build_user_prompt,
    parse_plan_json,
    plan_question,
    resolve_llm_settings,
)


def test_system_prompt_forbids_sql() -> None:
    assert "Never output SQL" in SYSTEM_PROMPT
    assert "LogicalPlan" in SYSTEM_PROMPT


def test_user_prompt_includes_catalog_not_rows() -> None:
    catalog = sample_catalog()
    prompt = build_user_prompt("revenue by country", catalog)
    assert "Invoice" in prompt
    assert "approved_joins" in prompt or "CustomerId" in prompt
    assert "no row data" in prompt.lower() or "Approved catalog" in prompt


def test_repair_prompt_forbids_sql() -> None:
    text = build_repair_prompt(["catalog.unknown_table: Unknown source table: 'foo'"])
    assert "Do not output SQL" in text
    assert "catalog.unknown_table" in text


def test_parse_plan_assigns_fresh_plan_id() -> None:
    raw = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "limit": 5,
        }
    )
    plan = parse_plan_json(raw)
    assert str(plan.plan_id) != "00000000-0000-0000-0000-000000000001"
    assert plan.source == "Invoice"


def test_parse_plan_rejects_sql_field() -> None:
    raw = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "sql": "SELECT * FROM Invoice",
            "limit": 5,
        }
    )
    with pytest.raises(ValueError, match="SQL"):
        parse_plan_json(raw)


def test_plan_question_ok_with_mock() -> None:
    catalog = sample_catalog()
    result = plan_question(
        "revenue by country",
        catalog,
        MockLLMClient(),
        max_repairs=1,
    )
    assert result.status == "ok"
    assert result.compiled is not None
    assert "SUM" in result.compiled.sql.upper()
    assert "Customer" in result.compiled.sql
    assert result.attempts == 1


def test_plan_question_one_repair_then_ok() -> None:
    bad = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "not_a_table",
            "limit": 10,
        }
    )
    good = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "joins": [
                {
                    "right_table": "Customer",
                    "kind": "inner",
                    "conditions": [
                        {
                            "left": {"table_id": "Invoice", "column_id": "CustomerId"},
                            "right": {"table_id": "Customer", "column_id": "CustomerId"},
                        }
                    ],
                }
            ],
            "filters": [],
            "group_by": {
                "columns": [{"table_id": "Customer", "column_id": "Country"}],
                "time_buckets": [],
            },
            "aggregations": [
                {
                    "fn": "sum",
                    "column": {"table_id": "Invoice", "column_id": "Total"},
                    "alias": "revenue",
                }
            ],
            "having": [],
            "order_by": [{"alias": "revenue", "direction": "desc"}],
            "limit": 10,
        }
    )
    client = MockLLMClient([bad, good])
    result = plan_question("revenue by country", sample_catalog(), client, max_repairs=1)
    assert result.status == "ok"
    assert result.attempts == 2
    assert len(client.calls) == 2
    assert "Do not output SQL" in client.calls[1][-1]["content"]


def test_plan_question_clarify_after_failed_repair() -> None:
    bad = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "not_a_table",
            "limit": 10,
        }
    )
    client = MockLLMClient([bad, bad])
    result = plan_question("anything", sample_catalog(), client, max_repairs=1)
    assert result.status == "clarify"
    assert result.compiled is None
    assert result.clarify_message
    assert result.attempts == 2
    assert any("unknown_table" in e for e in result.errors)


def test_resolve_llm_settings_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"
    assert "11434" in settings["base_url"]


def test_resolve_llm_settings_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURE_QUERY_PROVIDER", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "groq"
    assert "groq.com" in settings["base_url"]


def test_resolve_llm_settings_groq_ignores_ollama_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --provider groq must not send Ollama tag from .env to Groq API."""
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "qwen2.5:7b")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "groq"
    assert settings["model"] == "llama-3.3-70b-versatile"


def test_resolve_llm_settings_groq_keeps_valid_cloud_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "llama-3.3-70b-versatile")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["model"] == "llama-3.3-70b-versatile"


def test_resolve_llm_settings_ollama_ignores_groq_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "llama-3.3-70b-versatile")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"
    assert settings["model"] == "qwen2.5:7b"


def test_resolve_llm_settings_ollama_keeps_ollama_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "qwen2.5:7b")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["model"] == "qwen2.5:7b"


def test_resolve_llm_settings_none_is_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "SECURE_QUERY_PROVIDER",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "SECURE_QUERY_API_KEY",
        "SECURE_QUERY_USE_OLLAMA",
        "SECURE_QUERY_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "secure_query.planner.llm._ollama_reachable", lambda host=None: False
    )
    assert resolve_llm_settings() is None


def test_resolve_auto_detects_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SECURE_QUERY_PROVIDER",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "SECURE_QUERY_API_KEY",
        "SECURE_QUERY_USE_OLLAMA",
        "SECURE_QUERY_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "secure_query.planner.llm._ollama_reachable", lambda host=None: True
    )
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"


class TestRefusal:
    """The model must have a legal way to decline, or it will substitute an answer."""

    def test_refusal_is_reported_as_a_deliberate_decline(self) -> None:
        client = MockLLMClient(
            [json.dumps({"cannot_answer": True, "reason": "No Employee table in the catalog"})]
        )
        result = plan_question("How many employees?", sample_catalog(), client)
        assert result.status == "clarify"
        assert result.refused is True
        assert result.clarify_message == "No Employee table in the catalog"

    def test_refusal_does_not_consume_a_repair_attempt(self) -> None:
        """A refusal is final; retrying only pressures the model into guessing."""
        client = MockLLMClient(
            [json.dumps({"cannot_answer": True, "reason": "no such table"})]
        )
        result = plan_question("how many employees", sample_catalog(), client, max_repairs=3)
        assert result.attempts == 1
        assert len(client.calls) == 1

    def test_employee_headcount_via_metric(self) -> None:
        result = plan_question(
            "How many employees work for us?",
            sample_catalog(),
            MockLLMClient(),
            max_repairs=1,
        )
        assert result.status == "ok"
        assert result.compiled is not None
        assert "Employee" in result.compiled.sql
        assert "COUNT" in result.compiled.sql.upper()

    def test_restricted_questions_never_reach_the_model(self) -> None:
        """PII refusal is deterministic, so the question is not sent anywhere."""
        client = MockLLMClient()
        result = plan_question("list all customer emails", sample_catalog(), client)
        assert result.refused is True
        assert result.attempts == 0
        assert client.calls == []
        assert "Customer.Email" in (result.clarify_message or "")

    def test_guard_can_be_disabled_for_ablation(self) -> None:
        client = MockLLMClient()
        plan_question("list all customer emails", sample_catalog(), client, guard=False)
        assert client.calls, "guard=False should let the question through to the model"

    def test_invalid_json_is_not_treated_as_a_refusal(self) -> None:
        client = MockLLMClient(["not json at all", "still not json"])
        result = plan_question("anything", sample_catalog(), client, max_repairs=1)
        assert result.status == "clarify"
        assert result.refused is False


def test_openai_client_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from openai import RateLimitError

    from secure_query.planner import OpenAIClient

    sleeps: list[float] = []
    monkeypatch.setattr("secure_query.planner.llm.time.sleep", lambda s: sleeps.append(s))

    class FakeMessage:
        content = '{"cannot_answer": true, "reason": "test"}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    calls = {"n": 0}

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                response = MagicMock()
                response.request = MagicMock()
                raise RateLimitError(
                    "Rate limit reached. Please try again in 2.5s",
                    response=response,
                    body=None,
                )
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    client = OpenAIClient(api_key="test", provider="groq")
    client._client = FakeClient()

    text = client.complete([{"role": "user", "content": "hi"}])
    assert "cannot_answer" in text
    assert calls["n"] == 2
    assert sleeps == [2.5]


def _fake_openai_client(raise_exc: Exception | None, calls: list[dict]) -> object:
    class FakeMessage:
        content = '{"cannot_answer": true, "reason": "test"}'

    class FakeResponse:
        choices = [type("C", (), {"message": FakeMessage()})()]

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            if raise_exc is not None:
                raise raise_exc
            return FakeResponse()

    return type("FakeClient", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()


@pytest.mark.parametrize("status,error", [(400, "BadRequestError"), (401, "AuthenticationError")])
def test_openai_client_does_not_retry_client_errors(status: int, error: str) -> None:
    from unittest.mock import MagicMock

    import openai

    from secure_query.planner import OpenAIClient, PlannerError

    response = MagicMock(status_code=status)
    response.request = MagicMock()
    exc = getattr(openai, error)("bad", response=response, body=None)
    calls: list[dict] = []
    client = OpenAIClient(api_key="test", provider="openai")
    client._client = _fake_openai_client(exc, calls)
    with pytest.raises(PlannerError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(calls) == 1  # not retried, not re-sent without response_format


def test_openai_client_is_bounded() -> None:
    from secure_query.planner import OpenAIClient

    client = OpenAIClient(api_key="test", provider="openai")
    assert client._client.timeout == 30.0 and client._client.max_retries == 2
    calls: list[dict] = []
    client._client = _fake_openai_client(None, calls)
    client.complete([{"role": "user", "content": "hi"}])
    assert calls[0]["max_tokens"] == 1200 and "seed" in calls[0]


def test_plan_response_schema_is_strict_compatible() -> None:
    """OpenAI strict mode: every object closed with all properties required; no
    unsupported keywords; server-assigned fields absent; literal values are strings."""
    from secure_query.planner.response_schema import plan_response_format

    fmt = plan_response_format()
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]

    def walk(node):
        if isinstance(node, dict):
            assert not {"oneOf", "const", "default", "discriminator", "format"} & set(node)
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    plan = schema["$defs"]["LogicalPlan"]["properties"]
    assert not {"plan_id", "schema_version", "joins"} & set(plan)
    assert schema["$defs"]["LiteralValue"]["properties"]["value"] == {"type": "string"}


def test_structured_wrapper_parses_to_plans_metrics_and_refusals() -> None:
    from secure_query.planner import PlannerRefusal, parse_plan_json

    wrapped = {
        "kind": "plan",
        "plan": {
            "source": "Invoice",
            "filters": [
                {
                    "op": "gt",
                    "column": {"table_id": "Invoice", "column_id": "Total"},
                    "value": {"type": "integer", "value": "5"},
                }
            ],
            "group_by": None,
            "aggregations": [{"fn": "count", "column": None, "alias": "n"}],
            "having": [],
            "order_by": [],
            "limit": 1,
        },
        "metric_id": None,
        "limit": None,
        "reason": None,
    }
    plan = parse_plan_json(json.dumps(wrapped), sample_catalog())
    assert plan.filters[0].value.value == 5
    metric = parse_plan_json(
        json.dumps({"kind": "metric", "plan": None, "metric_id": "total_revenue", "limit": 1, "reason": None}),
        sample_catalog(),
    )
    assert metric.aggregations[0].alias == "total_revenue"
    with pytest.raises(PlannerRefusal):
        parse_plan_json(
            json.dumps({"kind": "refusal", "plan": None, "metric_id": None, "limit": None, "reason": "no"})
        )


def test_planner_sends_the_schema_only_to_structured_clients() -> None:
    from secure_query.planner.response_schema import plan_response_format

    seen: list[object] = []

    class Structured:
        structured_outputs = True

        def complete(self, messages, response_format=None):
            seen.append(response_format)
            return json.dumps({"kind": "refusal", "plan": None, "metric_id": None, "limit": None, "reason": "x"})

    plan_question("anything", sample_catalog(), Structured())
    assert seen == [plan_response_format()]
    mock = MockLLMClient()  # no structured_outputs: called with messages only
    plan_question("anything", sample_catalog(), mock)
    assert len(mock.calls) == 1


def test_prompt_teaches_the_wrapper_and_carries_no_shape_prose() -> None:
    """M6: the response shape has one source (the Pydantic-derived schema)."""
    from secure_query.planner.prompt import build_repair_prompt, system_prompt
    from secure_query.planner.response_schema import plan_response_format

    structured = system_prompt(True)
    for kind in ('"kind": "plan"', '"kind": "metric"', '"kind": "refusal"'):
        assert kind in structured
    for shape in ("Booking", "Hotel", '"op": "eq"', '"table_id"', '"time_buckets"', '{"cannot_answer"'):
        assert shape not in structured
    plain = system_prompt(False)
    schema = json.dumps(plan_response_format()["json_schema"]["schema"], separators=(",", ":"))
    assert plain.startswith(structured) and schema in plain
    assert "JSON response object" in build_repair_prompt(["x"])
