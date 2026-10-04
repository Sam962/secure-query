"""Ask a natural-language question against the Chinook sample (Phase 2+3).

Uses the same auth + execute routing as the HTTP API:
  - Identity from SECURE_QUERY_AUTH_MODE (dev / token / header)
  - DuckDB locally; Databricks or Postgres when those backends are configured

Providers:
  --provider ollama   (local; also auto-detected if Ollama is running)
  --provider groq     (needs GROQ_API_KEY)
  --provider openai   (needs OPENAI_API_KEY)
  --provider mock

Usage:
    pip install -e ".[dev,planner]"
    python -m secure_query.demo.load_chinook
    python -m secure_query.demo.ask --provider ollama "revenue by country"
"""

from __future__ import annotations

import argparse
import os
import sys

from secure_query.api.service import ask, production_auth_blocked
from secure_query.auth import (
    AuthError,
    resolve_principal,
)
from secure_query.demo.load_chinook import DUCKDB_PATH, load_sample_db
from secure_query.engine.execute import ExecuteOptions, ExecutionError
from secure_query.engine.runtime import runtime_config
from secure_query.planner import (
    MockLLMClient,
    OpenAIClient,
    PlannerError,
    default_client,
    resolve_llm_settings,
)


def _client_from_args(provider: str | None):
    if provider is None:
        client = default_client()
        if isinstance(client, MockLLMClient):
            print(
                "NOTE: no LLM provider selected and Ollama not detected — using mock.\n"
                "  Fix:  python -m secure_query.demo.ask --provider ollama \"...\"\n"
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

    blocked = production_auth_blocked()
    if blocked:
        print(f"ERROR: {blocked}", file=sys.stderr)
        return 2

    try:
        principal = resolve_principal()
    except AuthError as exc:
        print(f"ERROR: auth failed ({exc.status_code}): {exc}", file=sys.stderr)
        return 2

    try:
        client = _client_from_args(args.provider)
    except PlannerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    provider = getattr(client, "provider", type(client).__name__)
    model = getattr(client, "_model", None)

    print("--- question ---")
    print(question)
    print(f"principal: {principal.principal_id}  tenant: {principal.tenant_id}")
    print(f"execute backend: {config.backend}")
    detail = f"client: {type(client).__name__}  provider: {provider}"
    if model:
        detail += f"  model: {model}"
    print(detail)
    print()

    try:
        outcome = ask(
            question,
            principal=principal,
            config=config,
            catalog=config.catalog,
            client=client,
            confirm_only=args.confirm_only,
            execute=not args.confirm_only,
            options=ExecuteOptions(
                timeout_seconds=args.timeout,
                max_rows=args.max_rows,
                audit_path=config.audit_path,
            ),
        )
    except ExecutionError as exc:
        print(f"--- execute error ---\n{exc}")
        return 1

    if outcome.status == "clarify":
        print("--- clarify ---")
        if outcome.clarify_code:
            print(f"code: {outcome.clarify_code}")
        print(outcome.clarify_message)
        if outcome.suggestions:
            print()
            print("--- try one of these (review still required; nothing is rewritten for you) ---")
            for item in outcome.suggestions:
                print(f"  • {item.question}")
                print(f"    {item.reason}")
        return 1

    if outcome.explanation:
        print("--- what this will compute (confirm this matches your question) ---")
        print(outcome.explanation)
        print()
    if outcome.sql:
        print("--- SQL (compiled, not from LLM) ---")
        print(outcome.sql)
        print()
    if outcome.retrieved_tables:
        print(f"retrieved_tables: {', '.join(outcome.retrieved_tables)}")
        print()

    if outcome.status == "confirm":
        print("(confirm-only: not executed)")
        return 0

    print("--- results ---")
    if outcome.scale_note:
        print(outcome.scale_note)
    print(outcome.columns)
    for row in outcome.rows:
        print(tuple(row) if not isinstance(row, tuple) else row)
        print(tuple(row) if not isinstance(row, tuple) else row)
    print()
    print(
        f"plan_hash: {str(outcome.audit.get('plan_hash', ''))[:16]} ...  "
        f"backend: {outcome.audit.get('backend')}  "
        f"principal: {outcome.audit.get('principal_id')}  "
        f"audit: {config.audit_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
