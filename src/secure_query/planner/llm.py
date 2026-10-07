"""LLM clients and provider selection (Ollama / Groq / OpenAI / mock)."""

from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from typing import Any, Protocol

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)s", re.IGNORECASE)
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_DEFAULT_WAIT_S = 15.0
# Bounded calls: the SDK retries 429/5xx/connection errors itself (never 400/401).
_TIMEOUT_S = 30.0
_LOCAL_TIMEOUT_S = 120.0  # a local 7B model can take longer than a hosted API
_MAX_RETRIES = 2
_MAX_TOKENS = 1200
_SEED = 7


class LLMClient(Protocol):
    """Minimal chat client: messages in, text out.

    A client whose `structured_outputs` attribute is true also accepts
    `complete(messages, response_format=...)` (a json_schema the API enforces).
    """

    def complete(self, messages: list[dict[str, str]]) -> str: ...



class PlannerError(Exception):
    """Raised for planner configuration / transport failures (not validation)."""



class OpenAIClient:
    """OpenAI-compatible Chat Completions client (OpenAI, Groq, Ollama, …).

    Optional dependency: ``pip install -e '.[planner]'``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider: str = "openai",
        json_mode: bool | None = None,
        temperature: float = 0.0,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PlannerError(
                "Install the planner extra: pip install -e '.[planner]'"
            ) from exc

        self.provider = provider
        self.temperature = temperature
        # OpenAI enforces json_schema response formats; local/compat servers may not.
        self.structured_outputs = provider == "openai"
        self._model = model or "gpt-4o-mini"
        self._json_mode = (
            json_mode if json_mode is not None else provider in ("openai", "groq")
        )
        kwargs: dict[str, Any] = {
            "api_key": api_key or "unused",
            "timeout": _LOCAL_TIMEOUT_S if provider == "ollama" else _TIMEOUT_S,
            "max_retries": _MAX_RETRIES,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def complete(
        self, messages: list[dict[str, str]], response_format: dict[str, Any] | None = None
    ) -> str:
        from openai import APIConnectionError, APIError, APITimeoutError

        try:
            return self._complete(messages, response_format)
        except (APIConnectionError, APITimeoutError) as exc:
            raise PlannerError(f"LLM unreachable ({self.provider} {self._model}): {exc}") from exc
        except APIError as exc:
            raise PlannerError(f"LLM request failed ({self.provider} {self._model}): {exc}") from exc

    def _complete(
        self, messages: list[dict[str, str]], response_format: dict[str, Any] | None = None
    ) -> str:
        from openai import RateLimitError

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": _MAX_TOKENS,
            "seed": _SEED,
        }
        if response_format is not None and self.structured_outputs:
            create_kwargs["response_format"] = response_format
        elif self._json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}

        if self.provider == "openai":
            # One request; the SDK's bounded retries are the only retries.
            return self._content(self._client.chat.completions.create(**create_kwargs))

        for attempt in range(_RATE_LIMIT_MAX_RETRIES):
            try:
                try:
                    resp = self._client.chat.completions.create(**create_kwargs)
                except Exception as exc:
                    # Some local models reject response_format — retry without it.
                    if (
                        not isinstance(exc, RateLimitError)
                        and create_kwargs.pop("response_format", None) is not None
                    ):
                        resp = self._client.chat.completions.create(**create_kwargs)
                    else:
                        raise
            except RateLimitError as exc:
                if attempt >= _RATE_LIMIT_MAX_RETRIES - 1:
                    raise
                wait_s = _RATE_LIMIT_DEFAULT_WAIT_S
                match = _RETRY_AFTER_RE.search(str(exc))
                if match:
                    wait_s = float(match.group(1))
                time.sleep(wait_s)
                continue

            return self._content(resp)

        raise PlannerError("LLM rate limit retries exhausted")

    @staticmethod
    def _content(resp: Any) -> str:
        content = resp.choices[0].message.content
        if not content:
            raise PlannerError("LLM returned empty content")
        return content


class MockLLMClient:
    """Deterministic planner for tests / offline demos (no API key).

    Defaults to refusing rather than returning one canned plan for every question
    (which would score as confidently wrong on the accuracy suite).
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[list[dict[str, str]]] = []
        self.provider = "mock"

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self._responses:
            return self._responses.pop(0)
        question = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                question = msg.get("content", "")
                break
        return self._mock_response_for_question(question)

    def _mock_response_for_question(self, prompt: str) -> str:
        q = self._extract_question(prompt)
        sql_mode = "Question:\n" in prompt and "User question:\n" not in prompt
        if "revenue" in q and "country" in q and "billing" not in q:
            if sql_mode:
                return json.dumps({"sql": self._revenue_by_customer_country_sql()})
            return self._revenue_by_customer_country_plan()
        if q.startswith("how many invoices are there in total"):
            if sql_mode:
                return json.dumps({"sql": 'SELECT COUNT(*) AS n FROM "Invoice"'})
            return json.dumps({"metric_id": "invoice_count", "limit": 1})
        if q.startswith("what is the total revenue across all invoices"):
            if sql_mode:
                return json.dumps({"sql": 'SELECT SUM("Total") AS revenue FROM "Invoice"'})
            return json.dumps({"metric_id": "total_revenue", "limit": 1})
        if "employee" in q and ("how many" in q or "headcount" in q or "work for" in q):
            if sql_mode:
                return json.dumps({"sql": 'SELECT COUNT(*) AS n FROM "Employee"'})
            return json.dumps({"metric_id": "employee_count", "limit": 1})
        return json.dumps(
            {
                "cannot_answer": True,
                "reason": "Mock planner only handles a few demo patterns; use --provider ollama for full eval.",
            }
        )

    @staticmethod
    def _extract_question(prompt: str) -> str:
        """LQP prompts use 'User question:'; the SQL path uses 'Question:'."""
        for marker in ("User question:\n", "Question:\n"):
            if marker in prompt:
                rest = prompt.split(marker, 1)[1]
                return rest.split("\n\n", 1)[0].strip().lower()
        return prompt.strip().lower()

    @staticmethod
    def _revenue_by_customer_country_plan() -> str:
        return json.dumps(
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
                                "left": {
                                    "table_id": "Invoice",
                                    "column_id": "CustomerId",
                                },
                                "right": {
                                    "table_id": "Customer",
                                    "column_id": "CustomerId",
                                },
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

    @staticmethod
    def _revenue_by_customer_country_sql() -> str:
        return (
            'SELECT "Customer"."Country", SUM("Invoice"."Total") AS revenue '
            'FROM "Invoice" '
            'INNER JOIN "Customer" ON "Invoice"."CustomerId" = "Customer"."CustomerId" '
            'GROUP BY "Customer"."Country" ORDER BY revenue DESC LIMIT 10'
        )


_GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
_OLLAMA_DEFAULT_MODEL = "qwen2.5:7b"
_OLLAMA_MODEL_PREFIXES = (
    "qwen2.5",
    "llama3.2",
    "llama3.1",
    "llama3",
    "mistral",
    "phi3",
    "gemma2",
    "codellama",
    "deepseek-r1",
)
_GROQ_CLOUD_MODEL_PREFIXES = ("llama-3.", "llama-3-", "gpt-", "mixtral-", "gemma2-")


def _looks_like_ollama_model(model: str) -> bool:
    """True for local Ollama tags (qwen2.5:7b, llama3.2, …)."""
    name = model.strip().lower()
    if ":" in name:
        return True
    return any(name == p or name.startswith(p + ".") or name.startswith(p + ":") for p in _OLLAMA_MODEL_PREFIXES)


def _looks_like_groq_cloud_model(model: str) -> bool:
    """True for hosted Groq/OpenAI-style model ids (llama-3.3-70b-versatile, gpt-4o-mini, …)."""
    name = model.strip().lower()
    if name == _GROQ_DEFAULT_MODEL:
        return True
    return any(name.startswith(p) for p in _GROQ_CLOUD_MODEL_PREFIXES)


def _model_for_provider(provider: str, env_model: str | None, default: str) -> str:
    """Use env model when compatible with provider; else provider default."""
    if not env_model:
        return default
    env_model = env_model.strip()
    if provider == "groq" and _looks_like_ollama_model(env_model):
        return default
    if provider == "ollama" and _looks_like_groq_cloud_model(env_model):
        return default
    return env_model


def _ollama_reachable(host: str = "http://localhost:11434") -> bool:
    """True if a local Ollama server answers /api/tags."""
    import urllib.error
    import urllib.request

    url = host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ollama_model_digest(model: str, host: str | None = None) -> str | None:
    """Content digest of a local Ollama model. Tags like qwen2.5:7b can be re-pulled
    to a different build; the digest is the version an eval baseline should record."""
    import urllib.error
    import urllib.request

    base = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=2) as resp:
            models = json.loads(resp.read().decode("utf-8")).get("models", [])
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    names = {model, model if ":" in model else f"{model}:latest"}
    for entry in models:
        if entry.get("name") in names or entry.get("model") in names:
            digest = entry.get("digest")
            return str(digest) if digest else None
    return None


def resolve_llm_settings() -> dict[str, Any] | None:
    """Pick provider from env (or auto-detect local Ollama). None → MockLLMClient.

    Priority:
      1. SECURE_QUERY_PROVIDER=ollama|groq|openai
      2. GROQ_API_KEY → groq
      3. OPENAI_API_KEY / SECURE_QUERY_API_KEY → openai
      4. SECURE_QUERY_USE_OLLAMA=1 → ollama
      5. Ollama reachable on OLLAMA_HOST / localhost:11434 → ollama
    """
    provider = (os.environ.get("SECURE_QUERY_PROVIDER") or "").strip().lower()
    explicit_base = os.environ.get("SECURE_QUERY_BASE_URL")
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    if not provider:
        if os.environ.get("GROQ_API_KEY"):
            provider = "groq"
        elif os.environ.get("OPENAI_API_KEY") or os.environ.get("SECURE_QUERY_API_KEY"):
            provider = "openai"
        elif os.environ.get("SECURE_QUERY_USE_OLLAMA", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            provider = "ollama"
        elif explicit_base and "11434" in explicit_base:
            provider = "ollama"
        elif _ollama_reachable(ollama_host):
            provider = "ollama"
        else:
            return None

    if provider == "ollama":
        env_model = os.environ.get("SECURE_QUERY_MODEL") or os.environ.get("OLLAMA_MODEL")
        return {
            "provider": "ollama",
            "api_key": os.environ.get("SECURE_QUERY_API_KEY") or "ollama",
            "base_url": explicit_base or f"{ollama_host}/v1",
            "model": _model_for_provider("ollama", env_model, _OLLAMA_DEFAULT_MODEL),
            "json_mode": os.environ.get("SECURE_QUERY_JSON_MODE", "").lower()
            in ("1", "true", "yes"),
        }

    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY") or os.environ.get("SECURE_QUERY_API_KEY")
        if not key:
            raise PlannerError("Set GROQ_API_KEY for provider=groq")
        return {
            "provider": "groq",
            "api_key": key,
            "base_url": explicit_base or "https://api.groq.com/openai/v1",
            "model": _model_for_provider(
                "groq",
                os.environ.get("SECURE_QUERY_MODEL"),
                _GROQ_DEFAULT_MODEL,
            ),
            "json_mode": True,
        }

    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SECURE_QUERY_API_KEY")
        if not key:
            raise PlannerError("Set OPENAI_API_KEY for provider=openai")
        settings: dict[str, Any] = {
            "provider": "openai",
            "api_key": key,
            "model": os.environ.get("SECURE_QUERY_MODEL", "gpt-4o-mini"),
            "json_mode": True,
        }
        if explicit_base:
            settings["base_url"] = explicit_base
        return settings

    raise PlannerError(
        f"Unknown SECURE_QUERY_PROVIDER={provider!r} (use ollama|groq|openai)"
    )



def default_client() -> LLMClient:
    """Real LLM from env (Ollama / Groq / OpenAI); else MockLLMClient."""
    settings = resolve_llm_settings()
    if settings is None:
        return MockLLMClient()
    return OpenAIClient(**settings)


@lru_cache(maxsize=1)
def get_client() -> LLMClient:
    """Process-wide LLM client, so the SDK connection pool is reused. cache_clear() to reset."""
    return default_client()
