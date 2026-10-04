"""LLM planner: natural language → LogicalPlan JSON only (never SQL).

Flow:
    question + catalog.planner_summary()
      → LLM structured JSON
      → LogicalPlan (plan_id assigned locally)
      → validate_and_compile
      → on failure: one repair with error codes; then clarify

The model must not emit SQL. Repair prompts also must not include SQL.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.planner.guard import (
    dropped_average,
    dropped_concepts,
    opaque_grouping_keys,
    out_of_scope_request,
    restricted_request,
    useless_joins,
)
from secure_query.kernel.logical_plan import LogicalPlan
from secure_query.kernel.metrics import MetricSpec
from secure_query.kernel.validate import (
    PlanValidationFailed,
    normalize_plan,
    validate_and_compile,
    validate_and_compile_metric,
)

_SQL_LEAK_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|WITH)\b",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)s", re.IGNORECASE)
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_DEFAULT_WAIT_S = 15.0


SYSTEM_PROMPT = """You are a query planner for a secure analytics system.
You MUST output a single JSON object. Never output SQL.
Never wrap the JSON in markdown fences. Never include explanations outside JSON.

Output ONE of two objects:
1. A LogicalPlan, when the catalog can answer the question exactly.
2. A refusal, when it cannot:
   {"cannot_answer": true, "reason": "<one short sentence>"}

WHEN TO REFUSE — this matters more than being helpful. A refusal is always
better than a number that looks right but answers a different question.
Refuse if ANY of these is true:
- The question needs a table or column that is not in the catalog. Do NOT
  substitute a similar-sounding one. Check the catalog summary first: if the
  table is listed (Employee, Track, Genre, etc.), use it. If asked about
  suppliers, payroll, or inventory and the catalog has none, refuse — do not
  count customers instead.
- The question needs arithmetic *between* aggregates: a ratio, a percentage,
  a share of total, a growth rate, or an average "per" some entity other than
  the rows being aggregated. This IR has no division, so you cannot express it.
  Example: "average revenue per customer" means SUM(Total) / COUNT(DISTINCT
  CustomerId). AVG(Total) is the average per *invoice* — a different, wrong
  number. Refuse instead of using AVG.
- The question asks for a column marked [pii=high]. Do not return it, filter on
  it, group by it, or sort by it — and do not quietly answer a narrower
  question in its place. Refuse and say the field is restricted.
Never reuse an unavailable concept as an alias: do not alias a row count as
"email" or "payroll_total" to make the output look like what was asked for.

Rules:
- Use ONLY tables and columns listed in the catalog.
- Use ONLY approved joins from the catalog when joining.
- Always set limit (1–1000) unless the catalog forbids it; prefer 10 for top-N rankings.
  When the question asks for every/each/all categories (e.g. "each genre"), set limit
  high enough to return all groups (often 100–1000), not a top-10 default.
- To count child rows per parent (tracks per album, albums per artist), source from the
  child/detail table and join the parent for labels — never count rows on the parent alone.
- Every column ref must come from source or a joined table; to group by Artist.Name, join Artist.
- Column refs are objects: {"table_id": "...", "column_id": "..."}.
- Filter literals use LiteralValue: {"type": "string"|"integer"|"float"|"boolean", "value": ...}.
- Filters are a discriminated union on "op": eq, ne, lt, lte, gt, gte, in, not_in, between, is_null, not_null, like.
  Equality filter shape (required keys):
  {"op": "eq", "column": {"table_id": "Customer", "column_id": "Country"},
   "value": {"type": "string", "value": "USA"}}
  IN filter shape (note: "values" is a list of LiteralValue — not "value" with type list):
  {"op": "in", "column": {"table_id": "Customer", "column_id": "Country"},
   "values": [
     {"type": "string", "value": "Brazil"},
     {"type": "string", "value": "France"}
   ]}
  BETWEEN filter shape (note: "low" and "high" — not "values"):
  {"op": "between", "column": {"table_id": "Invoice", "column_id": "Total"},
   "low": {"type": "float", "value": 5.0}, "high": {"type": "float", "value": 10.0}}
  Date ranges on a date/datetime column: use two filters, gte the start and lt the
  day after the end, with ISO date literals. "in 2023" is:
  {"op": "gte", "column": {"table_id": "Invoice", "column_id": "InvoiceDate"},
   "value": {"type": "date", "value": "2023-01-01"}},
  {"op": "lt", "column": {"table_id": "Invoice", "column_id": "InvoiceDate"},
   "value": {"type": "date", "value": "2024-01-01"}}
  Do NOT use "left"/"right" for filters — those are only for join conditions.
  Do NOT use {"type": "list", "value": [...]} — that is invalid.
- group_by is either null or {"columns": [...], "time_buckets": []} — never a bare list.
- Time buckets group a date/datetime column into periods. Required keys are
  "column" (a ColumnRef) and "grain" (hour|day|week|month|quarter|year):
  {"column": {"table_id": "Invoice", "column_id": "InvoiceDate"}, "grain": "year"}
  There is no "unit" or "offset" key. When bucketing a date, put the column in
  the time_bucket and NOT also in "columns", or you will group by the raw timestamp.
- Aggregations: {"fn": "count"|"count_distinct"|"sum"|"avg"|"min"|"max", "column": ColumnRef|null, "alias": "..."}.
  For count(*), set "column": null.
- Do not invent tables, columns, or join keys.
- Do not include a "sql" field or any SQL strings.
- Include "schema_version": "lqp/1".
- Include "plan_id" as any UUID string (it will be replaced server-side).
- Alternatively return {"metric_id": "<approved_metric>", "limit": N} when a catalog
  approved_metric matches the question exactly (prefer this for revenue/count/ratio metrics).
- When the question asks "which artist/genre/album/playlist/media type", group by the
  table's display_column or FK label_for (e.g. Artist.Name), never a bare *_Id.
- To count tracks *sold*, source from InvoiceLine (line items), not Track.
- Country/city/year tokens in the question (USA, Brazil, London, 2010) are filter
  literals — use them in filter values; they are not unknown schema concepts.

Filter-and-list example shape:
{
  "plan_id": "00000000-0000-0000-0000-000000000002",
  "schema_version": "lqp/1",
  "source": "Customer",
  "joins": [],
  "filters": [{
    "op": "eq",
    "column": {"table_id": "Customer", "column_id": "Country"},
    "value": {"type": "string", "value": "USA"}
  }],
  "group_by": null,
  "aggregations": [],
  "having": [],
  "order_by": [{"column": {"table_id": "Customer", "column_id": "LastName"}, "direction": "asc"}],
  "limit": 20
}

Trend (per-period) example shape:
{
  "plan_id": "00000000-0000-0000-0000-000000000003",
  "schema_version": "lqp/1",
  "source": "Invoice",
  "joins": [],
  "filters": [],
  "group_by": {
    "columns": [],
    "time_buckets": [{"column": {"table_id": "Invoice", "column_id": "InvoiceDate"}, "grain": "year"}]
  },
  "aggregations": [{"fn": "count", "column": null, "alias": "invoice_count"}],
  "having": [],
  "order_by": [],
  "limit": 100
}

Minimal aggregate example shape:
{
  "plan_id": "00000000-0000-0000-0000-000000000001",
  "schema_version": "lqp/1",
  "source": "Invoice",
  "joins": [{
    "right_table": "Customer",
    "kind": "inner",
    "conditions": [{
      "left": {"table_id": "Invoice", "column_id": "CustomerId"},
      "right": {"table_id": "Customer", "column_id": "CustomerId"}
    }]
  }],
  "filters": [],
  "group_by": {"columns": [{"table_id": "Customer", "column_id": "Country"}], "time_buckets": []},
  "aggregations": [{"fn": "sum", "column": {"table_id": "Invoice", "column_id": "Total"}, "alias": "revenue"}],
  "having": [],
  "order_by": [{"alias": "revenue", "direction": "desc"}],
  "limit": 10
}
"""


class LLMClient(Protocol):
    """Minimal chat client: messages in, text out."""

    def complete(self, messages: list[dict[str, str]]) -> str: ...


@dataclass
class PlannerResult:
    """Outcome of plan_question."""

    status: Literal["ok", "clarify"]
    question: str
    plan: LogicalPlan | None = None
    compiled: CompiledQuery | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    clarify_message: str | None = None
    raw_responses: list[str] = field(default_factory=list)
    refused: bool = False
    """True when the model deliberately declined, vs. failing to produce a valid plan."""
    clarify_code: str | None = None
    metric: MetricSpec | None = None
    """Set for ratio/builtin metrics, which compile outside validate_and_compile."""
    refused_by_model: bool = False
    """The model declined. clarify_code is inferred from its wording (for the UI),
    so it must not be credited to a deterministic guard in eval scorecards."""


class PlannerError(Exception):
    """Raised for planner configuration / transport failures (not validation)."""


class PlannerRefusal(Exception):
    """The model declined to answer, which is a valid terminal outcome.

    Distinct from a validation failure: there is nothing to repair, so the
    planner must not burn a retry trying to force a plan out of the model.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PlannerError(
                "Install the planner extra: pip install -e '.[planner]'"
            ) from exc

        self.provider = provider
        self._model = model or "gpt-4o-mini"
        self._json_mode = (
            json_mode if json_mode is not None else provider in ("openai", "groq")
        )
        kwargs: dict[str, Any] = {"api_key": api_key or "unused"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def complete(self, messages: list[dict[str, str]]) -> str:
        from openai import APIConnectionError, APITimeoutError, RateLimitError

        try:
            return self._complete(messages)
        except (APIConnectionError, APITimeoutError) as exc:
            raise PlannerError(f"LLM unreachable ({self.provider} {self._model}): {exc}") from exc

    def _complete(self, messages: list[dict[str, str]]) -> str:
        from openai import RateLimitError

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
        }
        if self._json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}

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

            content = resp.choices[0].message.content
            if not content:
                raise PlannerError("LLM returned empty content")
            return content

        raise PlannerError("LLM rate limit retries exhausted")


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
        if "revenue" in q and "country" in q and "billing" not in q:
            return self._revenue_by_customer_country_plan()
        if q.startswith("how many invoices are there in total"):
            return json.dumps({"metric_id": "invoice_count", "limit": 1})
        if q.startswith("what is the total revenue across all invoices"):
            return json.dumps({"metric_id": "total_revenue", "limit": 1})
        if "employee" in q and ("how many" in q or "headcount" in q or "work for" in q):
            return json.dumps({"metric_id": "employee_count", "limit": 1})
        return json.dumps(
            {
                "cannot_answer": True,
                "reason": "Mock planner only handles a few demo patterns; use --provider ollama for full eval.",
            }
        )

    @staticmethod
    def _extract_question(prompt: str) -> str:
        marker = "User question:\n"
        if marker in prompt:
            return prompt.split(marker, 1)[1].strip().lower()
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


def build_user_prompt(question: str, catalog: Catalog) -> str:
    return (
        "Approved catalog (tables/columns/joins only — no row data):\n"
        f"{catalog.planner_summary()}\n\n"
        f"User question:\n{question}\n\n"
        "Return only the LogicalPlan JSON object."
    )


def build_repair_prompt(errors: list[str]) -> str:
    joined = "\n".join(f"- {e}" for e in errors)
    return (
        "The previous LogicalPlan failed validation. Fix the plan JSON only.\n"
        "Do not output SQL. Do not explain.\n"
        f"Validation errors:\n{joined}\n\n"
        "Return only the corrected LogicalPlan JSON object."
    )


def parse_plan_json(raw: str, catalog: Catalog | None = None) -> LogicalPlan:
    """Parse model JSON into LogicalPlan; assign a fresh plan_id locally."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Planner output must be a JSON object")
    if data.get("cannot_answer"):
        raise PlannerRefusal(str(data.get("reason") or "The planner declined to answer."))
    if "sql" in data or any(
        isinstance(v, str) and _SQL_LEAK_RE.search(v) for v in data.values() if isinstance(v, str)
    ):
        raise ValueError("Planner output must not contain SQL")
    if data.get("metric_id"):
        if catalog is None:
            raise ValueError("metric_id requires catalog to expand")
        from secure_query.kernel.metrics import (
            assert_metric_authorized,
            expand_metric_plan,
            metrics_for_catalog,
        )

        mid = str(data["metric_id"])
        metrics = {m.id: m for m in metrics_for_catalog(catalog)}
        if mid not in metrics:
            raise ValueError(f"Unknown metric_id: {mid!r}")
        metric = metrics[mid]
        try:
            assert_metric_authorized(metric, catalog)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        if metric.kind != "plan":
            raise ValueError(f"{metric.kind} metrics compile via try_compile_metric, not LogicalPlan")
        limit = data.get("limit")
        return expand_metric_plan(metric, limit=int(limit) if limit is not None else None)
    data["plan_id"] = str(uuid4())
    data.setdefault("schema_version", "lqp/1")
    return LogicalPlan.model_validate(data)


def try_compile_metric(
    raw: str, catalog: Catalog
) -> tuple[LogicalPlan | None, CompiledQuery | None, MetricSpec | None]:
    """Compile a ratio or builtin metric_id. Returns (plan, compiled, metric).

    Ratio metrics return their expanded plan so callers can inject row filters
    and recompile with validate_and_compile_metric. Builtin metrics have no plan.
    Anything else returns (None, None, None) and goes through parse_plan_json.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict) or not data.get("metric_id"):
        return None, None, None
    from secure_query.kernel.metrics import (
        assert_metric_authorized,
        compile_builtin_metric,
        expand_metric_plan,
        get_metric,
    )

    metric = get_metric(str(data["metric_id"]), catalog)
    if metric is None or metric.kind == "plan":
        return None, None, None
    try:
        assert_metric_authorized(metric, catalog)
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc

    if metric.kind == "ratio":
        limit = data.get("limit")
        plan = expand_metric_plan(metric, limit=int(limit) if limit is not None else None)
        return plan, validate_and_compile_metric(metric, plan, catalog), metric

    import hashlib

    assert metric.builder_id is not None
    sql = compile_builtin_metric(metric.builder_id, dialect=catalog.sql_dialect)
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()
    compiled = CompiledQuery(sql=sql, plan_hash=f"metric:{metric.id}", sql_hash=sql_hash, parameters=[])
    return None, compiled, metric


def plan_question(
    question: str,
    catalog: Catalog,
    client: LLMClient,
    *,
    max_repairs: int = 1,
    guard: bool = True,
    prompt_catalog: Catalog | None = None,
) -> PlannerResult:
    """Ask the LLM for a LogicalPlan, validate/compile, optionally one repair.

    `guard` enables the deterministic question checks. Leave it on outside of
    ablation experiments: they are the only refusals that do not depend on the
    model choosing to cooperate.

    `prompt_catalog` may be a retrieval slice for the LLM prompt. Validation
    always uses `catalog` (the full authorized allowlist).
    """
    from secure_query.planner.clarify import code_from_guard_message

    if max_repairs < 0:
        raise ValueError("max_repairs must be >= 0")

    summary_catalog = prompt_catalog or catalog

    if guard:
        blocked = restricted_request(question, catalog)
        if blocked is not None:
            return PlannerResult(
                status="clarify",
                question=question,
                attempts=0,
                clarify_message=blocked,
                refused=True,
                clarify_code="restricted_pii",
            )
        oos = out_of_scope_request(question, catalog)
        if oos is not None:
            return PlannerResult(
                status="clarify",
                question=question,
                attempts=0,
                clarify_message=oos,
                refused=True,
                clarify_code="out_of_scope",
            )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, summary_catalog)},
    ]
    errors: list[str] = []
    raw_responses: list[str] = []
    attempts = 0

    while True:
        attempts += 1
        raw = client.complete(messages)
        raw_responses.append(raw)
        try:
            metric_plan, metric_compiled, metric = try_compile_metric(raw, catalog)
            if metric_compiled is not None:
                return PlannerResult(
                    status="ok",
                    question=question,
                    plan=metric_plan,
                    compiled=metric_compiled,
                    metric=metric,
                    attempts=attempts,
                    errors=[],
                    raw_responses=raw_responses,
                )
            plan = normalize_plan(parse_plan_json(raw, catalog), catalog)
            compiled = validate_and_compile(plan, catalog)
            if guard:
                gap = (
                    useless_joins(plan)
                    or opaque_grouping_keys(question, plan, catalog)
                    or dropped_concepts(question, plan, catalog)
                    or dropped_average(question, plan)
                )
                if gap is not None:
                    return PlannerResult(
                        status="clarify",
                        question=question,
                        attempts=attempts,
                        errors=errors,
                        clarify_message=gap,
                        raw_responses=raw_responses,
                        refused=True,
                        clarify_code=code_from_guard_message(gap, refused=True),
                    )
            return PlannerResult(
                status="ok",
                question=question,
                plan=plan,
                compiled=compiled,
                attempts=attempts,
                errors=[],
                raw_responses=raw_responses,
            )
        except PlannerRefusal as refusal:
            return PlannerResult(
                status="clarify",
                question=question,
                attempts=attempts,
                errors=errors,
                clarify_message=refusal.reason,
                raw_responses=raw_responses,
                refused=True,
                clarify_code=code_from_guard_message(refusal.reason, refused=True)
                or "planner_refusal",
                refused_by_model=True,
            )
        except (json.JSONDecodeError, ValueError, PlanValidationFailed) as exc:
            if isinstance(exc, PlanValidationFailed):
                err_msgs = [f"{e.code}: {e.message}" for e in exc.errors]
            else:
                err_msgs = [str(exc)]
            errors.extend(err_msgs)
            if attempts > max_repairs:
                return PlannerResult(
                    status="clarify",
                    question=question,
                    attempts=attempts,
                    errors=errors,
                    clarify_message=(
                        "Could not produce a valid LogicalPlan after "
                        f"{attempts} attempt(s). Please rephrase or narrow the question."
                    ),
                    raw_responses=raw_responses,
                    clarify_code="validation_failed",
                )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": build_repair_prompt(err_msgs)})


def default_client() -> LLMClient:
    """Real LLM from env (Ollama / Groq / OpenAI); else MockLLMClient."""
    settings = resolve_llm_settings()
    if settings is None:
        return MockLLMClient()
    return OpenAIClient(**settings)
