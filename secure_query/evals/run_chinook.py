"""Run Chinook Phase 4 goldens (and optional live LLM eval).

Usage:
    python -m secure_query.evals.run_chinook
    python -m secure_query.evals.run_chinook --live --provider ollama
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from secure_query.evals import (
    iter_cases,
    load_case,
    run_golden_case,
    score_live_plan,
)
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner import (
    MockLLMClient,
    OpenAIClient,
    PlannerError,
    default_client,
    ollama_model_digest,
    plan_question,
    resolve_llm_settings,
)


def _client(provider: str | None):
    if provider is None:
        return default_client()
    provider = provider.lower()
    if provider == "mock":
        return MockLLMClient()
    os.environ["SECURE_QUERY_PROVIDER"] = provider
    settings = resolve_llm_settings()
    if settings is None:
        raise PlannerError(f"Could not resolve provider={provider}")
    return OpenAIClient(**settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chinook golden / live eval runner")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also score live planner on case questions (needs LLM or mock)",
    )
    parser.add_argument(
        "--accuracy",
        action="store_true",
        help="Run the execution-accuracy suite: NL question -> rows vs reference SQL",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Ask each accuracy question N times to measure planner consistency",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Limit the accuracy suite to case ids/tags containing this string",
    )
    parser.add_argument(
        "--split",
        choices=("dev", "holdout", "all"),
        default="all",
        help="Eval split: dev (tune here), holdout (release gate), all",
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "groq", "openai", "mock"),
        default=None,
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Override SECURE_QUERY_MODEL. Ollama default is qwen2.5:7b "
            "(llama3.2 is too weak for LogicalPlan JSON)."
        ),
    )
    parser.add_argument(
        "--expect-digest",
        default=os.environ.get("SECURE_QUERY_MODEL_DIGEST") or None,
        metavar="PREFIX",
        help=(
            "Refuse to run unless the Ollama model digest starts with PREFIX "
            "(default: SECURE_QUERY_MODEL_DIGEST). Tags can be re-pulled to a new build."
        ),
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="Also write the accuracy report (summary + per-case verdicts) as JSON",
    )
    parser.add_argument(
        "--fail-on-wrong",
        action="store_true",
        help="Exit non-zero if any wrong/unsafe answers (use with --split holdout in CI)",
    )
    parser.add_argument(
        "--case-delay",
        type=float,
        default=None,
        metavar="SECS",
        help="Pause between accuracy cases (default: 1.5 for groq, else 0)",
    )
    args = parser.parse_args(argv)
    if args.model:
        os.environ["SECURE_QUERY_MODEL"] = args.model

    cases = iter_cases()
    if not cases:
        print("No Chinook eval cases found", file=sys.stderr)
        return 2

    print(f"=== Goldens ({len(cases)} cases) ===")
    golden_results = [run_golden_case(c) for c in cases]
    for result in golden_results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"  [{mark}] {result.case_id}")
        for err in result.errors:
            print(f"         {err}")
    golden_fail = sum(1 for r in golden_results if not r.passed)
    print(f"golden: {len(golden_results) - golden_fail}/{len(golden_results)} passed")

    live_fail = 0
    if args.live:
        print()
        print("=== Live planner (structural) ===")
        try:
            client = _client(args.provider)
        except PlannerError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        provider = getattr(client, "provider", type(client).__name__)
        model = getattr(client, "_model", None)
        print(f"provider={provider}" + (f" model={model}" if model else ""))
        catalog = sample_catalog()
        for case_dir in cases:
            meta, _golden_plan = load_case(case_dir)
            if not meta.get("expect_validate_ok", True):
                print(f"  [SKIP] {meta['id']} (negative / policy case)")
                continue
            if isinstance(client, MockLLMClient) and meta["id"] != "revenue_by_country":
                print(f"  [SKIP] {meta['id']} (mock only covers revenue_by_country)")
                continue
            result = plan_question(meta["question"], catalog, client, max_repairs=1)
            errors: list[str] = []
            if result.status != "ok" or result.plan is None:
                errors.append(result.clarify_message or "planner clarify")
                errors.extend(result.errors)
            else:
                errors.extend(score_live_plan(meta, result.plan))
            ok = not errors
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {meta['id']}")
            for err in errors:
                print(f"         {err}")
            if not ok:
                live_fail += 1
                if result.raw_responses:
                    snippet = result.raw_responses[-1][:400].replace("\n", " ")
                    print(f"         raw: {snippet}...")

    accuracy_fail = 0
    if args.accuracy:
        accuracy_fail = _run_accuracy(args)
        if accuracy_fail < 0:
            return 2

    if args.fail_on_wrong and accuracy_fail > 0:
        return 1

    return 1 if (golden_fail + live_fail + accuracy_fail) else 0


_MARKS = {
    "correct": "OK  ",
    "wrong": "WRONG",
    "unsafe": "UNSAFE",
    "abstained": "abstain",
    "error": "ERROR",
}


def _run_accuracy(args: argparse.Namespace) -> int:
    """Execution-accuracy run. Returns count of dangerous outcomes, or -1 on setup failure."""
    from secure_query.evals.accuracy import (
        load_live_suite,
        run_live_case,
        summarise,
    )
    from secure_query.examples.load_sample_db import DUCKDB_PATH

    print()
    print(f"=== Execution accuracy (split={args.split}) ===")
    if not DUCKDB_PATH.exists():
        print(f"ERROR: sample DB missing at {DUCKDB_PATH}", file=sys.stderr)
        return -1
    try:
        client = _client(args.provider)
    except PlannerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return -1

    provider = getattr(client, "provider", type(client).__name__)
    model = getattr(client, "_model", None)
    digest = ollama_model_digest(model) if provider == "ollama" and model else None
    print(
        f"provider={provider}"
        + (f" model={model}" if model else "")
        + (f" digest={digest[:12]}" if digest else "")
        + f"  repeats={args.repeat}"
    )
    if args.expect_digest and provider != "mock":
        if digest is None or not digest.startswith(args.expect_digest):
            print(
                f"ERROR: model digest {digest[:12] if digest else 'unknown'} does not match "
                f"pinned {args.expect_digest}; results would not be comparable to the baseline",
                file=sys.stderr,
            )
            return -1

    catalog = sample_catalog()
    suite = load_live_suite(split=args.split)
    if args.only:
        needles = [n.strip() for n in args.only.split(",") if n.strip()]
        suite = [
            c
            for c in suite
            if any(n in c.case_id or n in c.tags for n in needles)
        ]
    case_delay = args.case_delay
    if case_delay is None:
        case_delay = 1.5 if provider == "groq" else 0.0

    results = []
    for i, case in enumerate(suite):
        if i and case_delay > 0:
            time.sleep(case_delay)
        result = run_live_case(case, catalog, client, DUCKDB_PATH, repeats=args.repeat)
        results.append(result)
        mark = _MARKS.get(result.verdict, result.verdict)
        consistency = ""
        if args.repeat > 1:
            # Refusals produce no plan, so there is no shape to agree on.
            consistency = (
                f" [{result.consistency:.0%} stable]" if result.consistency else " [no plan]"
            )
        print(f"  [{mark:7}] {case.case_id}{consistency}")
        if result.verdict != "correct":
            print(f"            {result.attempts[-1].detail}")
            if result.attempts[-1].sql:
                print(f"            sql: {result.attempts[-1].sql}")

    stats = summarise(results)
    low, high = stats["wrong_rate_ci95"]
    print()
    print(
        f"accuracy      {stats['accuracy']:.0%}  ({stats['correct']}/{stats['total']})\n"
        f"wrong answers {stats['wrong_rate']:.0%}  ({stats['wrong'] + stats['unsafe']}/{stats['total']})"
        f"   95% CI {low:.1%}–{high:.1%}   <- confident and incorrect\n"
        f"abstained     {stats['abstain_rate']:.0%}  ({stats['abstained']}/{stats['total']})"
        "   <- safe: declined to answer"
    )
    if stats["answerable"]:
        print(
            f"answer-rate   {stats['answer_rate']:.0%}  of {stats['answerable']} answerable"
            f"   (over-refusal {stats['over_refusal_rate']:.0%})"
        )
    if stats["error"]:
        print(f"errors        {stats['error']}")
    if args.repeat > 1:
        print(f"consistency   {stats['consistency']:.0%}  (same plan shape across repeats)")
    if stats["by_refusal_code"]:
        print("refusals by control   (correct = should decline, false = blocked a real answer)")
        for code, counts in stats["by_refusal_code"].items():
            print(f"  {code:20} correct={counts.get('correct', 0):3}  false={counts.get('false', 0):3}")
    print("by tag")
    for tag, counts in stats["by_tag"].items():
        n = sum(counts.values())
        print(
            f"  {tag:20} n={n:3}  correct={counts.get('correct', 0):3}"
            f"  wrong={counts.get('wrong', 0) + counts.get('unsafe', 0):3}"
            f"  abstained={counts.get('abstained', 0):3}"
        )
    if args.json:
        _write_json_report(args.json, stats, results, provider=provider, model=model, digest=digest)
    return stats["wrong"] + stats["unsafe"]


def _write_json_report(path, stats, results, *, provider, model, digest) -> None:
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "model_digest": digest,
        "summary": stats,
        "cases": [
            {
                "id": r.case.case_id,
                "expect": r.case.expect,
                "tags": list(r.case.tags),
                "verdict": r.verdict,
                "refusal_code": next(
                    (a.refusal_code for a in reversed(r.attempts) if a.refusal_code), None
                ),
                "detail": r.attempts[-1].detail if r.attempts else None,
                "sql": r.attempts[-1].sql if r.attempts else None,
            }
            for r in results
        ],
    }
    Path(path).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
