"""Planner tests: LLM clients and the SQL planner, mock LLM only (no network)."""

from __future__ import annotations

import json

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.planner import MockLLMClient, plan_sql_question, resolve_llm_settings


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
        result = plan_sql_question("How many employees?", sample_catalog(), client)
        assert result.status == "clarify"
        assert result.refused is True
        assert result.clarify_message == "No Employee table in the catalog"

    def test_refusal_does_not_consume_a_repair_attempt(self) -> None:
        """A refusal is final; retrying only pressures the model into guessing."""
        client = MockLLMClient(
            [json.dumps({"cannot_answer": True, "reason": "no such table"})]
        )
        result = plan_sql_question("how many employees", sample_catalog(), client, max_repairs=3)
        assert result.attempts == 1
        assert len(client.calls) == 1

    def test_invalid_json_is_not_treated_as_a_refusal(self) -> None:
        client = MockLLMClient(["not json at all", "still not json"])
        result = plan_sql_question("anything", sample_catalog(), client, max_repairs=1)
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
    assert calls[0]["max_completion_tokens"] == 1200 and "seed" in calls[0]


def test_openai_client_adapts_to_parameters_a_model_rejects() -> None:
    """Any model should work: a parameter the server rejects by name is dropped and remembered."""
    from unittest.mock import MagicMock

    import openai

    from secure_query.planner import OpenAIClient

    def bad(message: str) -> Exception:
        response = MagicMock(status_code=400)
        response.request = MagicMock()
        return openai.BadRequestError(message, response=response, body=None)

    calls: list[dict] = []
    client = OpenAIClient(api_key="test", provider="groq", temperature=0.7)
    fake = _fake_openai_client(None, calls)
    real_create = fake.chat.completions.create

    def create(**kwargs: object) -> object:
        if "max_tokens" in kwargs:
            calls.append(kwargs)
            raise bad("Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.")
        if "temperature" in kwargs:
            calls.append(kwargs)
            raise bad("Unsupported value: 'temperature' does not support 0.7 with this model.")
        return real_create(**kwargs)

    fake.chat.completions.create = create
    client._client = fake
    client.complete([{"role": "user", "content": "hi"}])
    assert len(calls) == 3
    assert calls[-1]["max_completion_tokens"] == 1200 and "seed" in calls[-1]
    assert "temperature" not in calls[-1] and "max_tokens" not in calls[-1]
    client.complete([{"role": "user", "content": "hi"}])
    assert len(calls) == 4  # remembered: the second request is right first time


def test_mock_extracts_the_sql_path_question() -> None:
    from secure_query.planner.sql_plan import build_sql_prompt, plan_sql_question

    catalog = sample_catalog()
    prompt = build_sql_prompt("revenue by country", catalog)
    assert MockLLMClient._extract_question(prompt) == "revenue by country"
    result = plan_sql_question("revenue by country", catalog, MockLLMClient())
    assert result.status == "ok" and result.compiled is not None
    assert "Customer" in result.compiled.sql and "SUM" in result.compiled.sql.upper()


def test_mock_sql_path_unknown_question_is_a_refusal() -> None:
    from secure_query.planner.sql_plan import plan_sql_question

    result = plan_sql_question("payroll totals", sample_catalog(), MockLLMClient())
    assert result.status == "clarify" and result.refused



def test_sql_prompt_carries_owner_metric_definitions() -> None:
    from secure_query.planner.sql_plan import build_sql_prompt

    prompt = build_sql_prompt("average revenue per customer?", sample_catalog())
    assert "avg_revenue_per_customer: Total revenue divided by distinct customers" in prompt
    assert "SUM(Invoice.Total) / COUNT(DISTINCT Invoice.CustomerId) over Invoice" in prompt
    assert "per Customer.Country; joining Invoice.CustomerId=Customer.CustomerId" in prompt
