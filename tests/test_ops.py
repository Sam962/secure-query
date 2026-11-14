"""Operational surface: rate limit, metrics, request id, model-provider breaker."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from secure_query.api.ops import Metrics, RateLimiter, limiter, metrics
from tests.conftest import requires_chinook


def test_rate_limiter_refills_and_is_per_person() -> None:
    bucket = RateLimiter()
    assert [bucket.retry_after("alice", 2, now=0.0) for _ in range(2)] == [0.0, 0.0]
    assert bucket.retry_after("alice", 2, now=0.0) == pytest.approx(30.0)
    assert bucket.retry_after("bob", 2, now=0.0) == 0.0
    assert bucket.retry_after("alice", 2, now=31.0) == 0.0
    assert bucket.retry_after("alice", 0, now=31.0) == 0.0  # 0 turns the limit off


def test_metrics_render_prometheus_text() -> None:
    m = Metrics()
    m.count("secure_query_asks_total", status="ok", code="none")
    m.count("secure_query_asks_total", status="ok", code="none")
    m.observe("/ask", 0.3)
    text = m.render()
    assert 'secure_query_asks_total{code="none",status="ok"} 2' in text
    assert 'secure_query_request_seconds_bucket{route="/ask",le="0.5"} 1' in text
    assert 'secure_query_request_seconds_bucket{route="/ask",le="0.25"} 0' in text
    assert 'secure_query_request_seconds_count{route="/ask"} 1' in text


@requires_chinook
def test_api_limits_each_person_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.api.http import app
    from secure_query.planner import MockLLMClient

    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    monkeypatch.setenv("SECURE_QUERY_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("SECURE_QUERY_METRICS_TOKEN", "scrape-me")
    limiter.reset()
    metrics.reset()
    with TestClient(app) as client:
        codes = [client.post("/ask/confirm", json={"question": "revenue by country"}).status_code for _ in range(3)]
        assert codes[:2] == [200, 200] and codes[2] == 429
        limited = client.post("/ask/confirm", json={"question": "revenue by country"}, headers={"X-Request-ID": "abc-123"})
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
        assert limited.headers["X-Request-ID"] == "abc-123"
        assert client.get("/health").headers["X-Request-ID"]

        assert client.get("/metrics").status_code == 401
        text = client.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).text
    assert 'secure_query_requests_total{route="/ask/confirm",status="429"} 2' in text
    assert "secure_query_rate_limited_total 2" in text
    limiter.reset()


def test_breaker_stops_calling_a_failing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    import httpx
    from openai import APIConnectionError

    from secure_query.planner.llm import PlannerError, _Breaker

    monkeypatch.setenv("SECURE_QUERY_LLM_BREAKER_FAILURES", "2")
    breaker = _Breaker(cooloff=60.0)
    calls = []

    def down():
        calls.append(1)
        raise APIConnectionError(request=httpx.Request("POST", "http://model.invalid"))

    for _ in range(2):
        with pytest.raises(APIConnectionError):
            breaker.call(down)
    with pytest.raises(PlannerError, match="failing"):
        breaker.call(down)
    assert len(calls) == 2  # the third call never reached the provider

    breaker._open_until = 0.0  # cool-off over: one call goes through and closes the breaker
    assert breaker.call(lambda: "ok") == "ok" and breaker._failures == 0
