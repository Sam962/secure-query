"""Ask a natural-language question against the Chinook sample (Phase 2+3).

Uses the same auth + execute routing as the HTTP API:
  - Identity from SECURE_QUERY_AUTH_MODE (dev / token / header)
  - DuckDB locally, Databricks when DATABRICKS_* env vars are set

Providers:
  --provider ollama   (local; also auto-detected if Ollama is running)
  --provider groq     (needs GROQ_API_KEY)
  --provider openai   (needs OPENAI_API_KEY)
  --provider mock

Usage:
    pip install -e ".[dev,planner]"
    python -m secure_query.examples.load_sample_db
    python -m secure_query.examples.ask_sample --provider ollama "revenue by country"
"""

from __future__ import annotations

import argparse
import os
import sys

from secure_query.auth import (
    AuthError,
    assert_ratio_allowed,
    catalog_for_principal,
    inject_row_filters,
    resolve_principal,
)
from secure_query.examples.load_sample_db import DUCKDB_PATH, load_sample_db
from secure_query.execute import ExecuteOptions, ExecutionError
from secure_query.explain import explain_plan
from secure_query.planner import (
    MockLLMClient,
    OpenAIClient,
    PlannerError,
    default_client,
    plan_question,
    resolve_llm_settings,
)
from secure_query.runtime import execute_compiled_query, runtime_config
from secure_query.validate import PlanValidationFailed, validate_and_compile


def _client_from_args(provider: str | None):
    if provider is None:
        client = default_client()
        if isinstance(client, MockLLMClient):
            print(
                "NOTE: no LLM provider selected and Ollama not detected — using mock.\n"
                "  Fix:  python -m secure_query.examples.ask_sample --provider ollama \"...\"\n"
                "  Or:   export SECURE_QUERY_PROVIDER=ollama\n",
                file=sys.stderr,
            )
        return client

    provider = provider.lower().strip()
    if provider == "mock":
        return MockLLMClient()

    os.environ["SECURE_QUERY_PROVIDER"] = provider
    settings = resolve_llm_settings()
    if settings is None:
        raise PlannerError(f"Could not resolve settings for provider={provider!r}")
    return OpenAIClient(**settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Secure-query NL → LogicalPlan → execute")
    parser.add_argument(
        "--provider",
        choices=("ollama", "groq", "openai", "mock"),
        default=None,
        help="LLM provider (default: env / auto-detect Ollama / mock)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=1000,
        help="Execute-time row cap (default 1000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Execute timeout seconds (default 30)",
    )
    parser.add_argument(
        "--confirm-only",
        action="store_true",
        help="Show explain-back + SQL only; do not execute",
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="Natural language question",
    )
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip() or (
        "What is total invoice revenue by customer country?"
    )

    config = runtime_config()
    if config.backend == "duckdb" and not DUCKDB_PATH.exists():
        load_sample_db(prefer_download=True)

    try:
        principal = resolve_principal()
    except AuthError as exc:
        print(f"ERROR: auth failed ({exc.status_code}): {exc}", file=sys.stderr)
        return 2

    try:
        catalog = catalog_for_principal(config.catalog, principal)
    except PermissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        client = _client_from_args(args.provider)
    except PlannerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    planned = plan_question(question, catalog, client, max_repairs=1)

    provider = getattr(client, "provider", type(client).__name__)
    model = getattr(client, "_model", None)

    print("--- question ---")
    print(planned.question)
    print(f"attempts: {planned.attempts}  status: {planned.status}")
    print(f"principal: {principal.principal_id}  tenant: {principal.tenant_id}")
    print(f"execute backend: {config.backend}")
    detail = f"client: {type(client).__name__}  provider: {provider}"
    if model:
        detail += f"  model: {model}"
    print(detail)
    print()

    if planned.status != "ok" or (planned.compiled is None and planned.plan is None):
        print("--- clarify ---")
        print(planned.clarify_message)
        print("errors:")
        for e in planned.errors:
            print(f"  - {e}")
        if planned.raw_responses:
            print("--- raw model output (last) ---")
            print(planned.raw_responses[-1][:2000])
        return 1

    plan = planned.plan
    compiled = planned.compiled

    if plan is not None:
        plan = inject_row_filters(plan, principal)
        try:
            compiled = validate_and_compile(plan, catalog)
        except PlanValidationFailed as exc:
            print("--- clarify ---")
            print(str(exc))
            return 1
    elif compiled is None:
        print("--- clarify ---")
        print("No plan or compiled SQL produced")
        return 1
    else:
        try:
            assert_ratio_allowed(principal)
        except PermissionError as exc:
            print("--- clarify ---")
            print(str(exc))
            return 1

    assert compiled is not None

    if plan is not None:
        print("--- what this will compute (confirm this matches your question) ---")
        print(explain_plan(plan, catalog))
        print()
        print("--- logical plan (JSON) ---")
        print(plan.to_json())
        print()
    print("--- SQL (compiled, not from LLM) ---")
    print(compiled.sql)
    print()

    if args.confirm_only:
        print("(confirm-only: not executed)")
        return 0

    try:
        exec_result = execute_compiled_query(
            compiled,
            config,
            plan=plan,
            question=question,
            principal=principal,
            options=ExecuteOptions(
                timeout_seconds=args.timeout,
                max_rows=min(args.max_rows, catalog.max_limit),
                audit_path=config.audit_path,
            ),
        )
    except ExecutionError as exc:
        print(f"--- execute error ---\n{exc}")
        return 1

    print("--- results ---")
    print(exec_result.columns)
    for row in exec_result.rows:
        print(row)
    if exec_result.truncated:
        print(f"(truncated at max_rows={args.max_rows})")
    print()
    print(
        f"plan_hash: {exec_result.audit.plan_hash[:16]} ...  "
        f"duration_ms: {exec_result.duration_ms:.1f}  "
        f"backend: {exec_result.audit.backend}  "
        f"principal: {exec_result.audit.principal_id}  "
        f"audit: {config.audit_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
