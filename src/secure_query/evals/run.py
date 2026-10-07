"""Execution-accuracy eval: ask each question, run the plan, compare with reference SQL.

Usage:
    python -m secure_query.evals.run --split holdout --provider ollama
    python -m secure_query.evals.run --suite northwind

Exit status: 0 = no wrong or unsafe answers, 1 = at least one, 2 = setup error.
Golden plan→SQL cases run under pytest (test_chinook_evals), not here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.catalog import Catalog
from secure_query.planner import (
    MockLLMClient,
    OpenAIClient,
    PlannerError,
    default_client,
    ollama_model_digest,
    resolve_llm_settings,
)


def _client(provider: str | None, temperature: float = 0.0):
    if provider is None and temperature == 0.0:
        return default_client()
    provider = (provider or os.environ.get("SECURE_QUERY_PROVIDER") or "ollama").lower()
    if provider == "mock":
        return MockLLMClient()
    os.environ["SECURE_QUERY_PROVIDER"] = provider
    settings = resolve_llm_settings()
    if settings is None:
        raise PlannerError(f"Could not resolve provider={provider}")
    return OpenAIClient(**settings, temperature=temperature)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execution-accuracy eval runner")
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
        "--suite",
        default=None,
        metavar="PATH",
        help=(
            "Accuracy suite JSON for another domain (e.g. northwind). "
            "Its 'catalog' and 'database' keys replace Chinook; --split is ignored"
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases")
    parser.add_argument(
        "--planner", choices=("lqp", "sql"), default="lqp",
        help="lqp: model writes a LogicalPlan; sql: model writes SQL, kernel validates it",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Also sample N plans per case at --sample-temperature and log them (self-consistency)",
    )
    parser.add_argument("--sample-temperature", type=float, default=0.7)
    parser.add_argument(
        "--sample-cases", type=int, default=None, metavar="N",
        help="Evaluate a seeded random subset of N cases",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Cases evaluated concurrently (the LLM server must allow parallel requests)",
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
        "--case-delay",
        type=float,
        default=None,
        metavar="SECS",
        help="Pause between accuracy cases (default: 1.5 for groq, else 0)",
    )
    args = parser.parse_args(argv)
    if args.model:
        os.environ["SECURE_QUERY_MODEL"] = args.model

    wrong = _run_accuracy(args)
    if wrong < 0:
        return 2
    return 1 if wrong else 0


_MARKS = {
    "correct": "OK  ",
    "wrong": "WRONG",
    "unsafe": "UNSAFE",
    "abstained": "abstain",
    "error": "ERROR",
}


def _run_accuracy(args: argparse.Namespace) -> int:
    """Execution-accuracy run. Returns count of dangerous outcomes, or -1 on setup failure."""
    from secure_query.demo.load_chinook import DUCKDB_PATH
    from secure_query.evals.accuracy import (
        run_live_case,
        summarise,
    )
    from secure_query.evals.suite_loader import load_suite

    if args.suite and args.suite.startswith("spider-"):
        from secure_query.evals.spider import load_spider

        items = load_spider(args.suite.removeprefix("spider-"))
    elif args.suite:
        catalog, db_path, suite = _load_domain_suite(args.suite)
        items = [(case, catalog, db_path) for case in suite]
    else:
        items = [(case, sample_catalog(), DUCKDB_PATH) for case in load_suite(split=args.split)]
    print(f"=== Execution accuracy ({args.suite or 'chinook split=' + args.split}) ===")
    missing = {str(db) for _, _, db in items if not db.exists()}
    if missing:
        print(f"ERROR: database missing: {sorted(missing)[:3]}", file=sys.stderr)
        return -1
    try:
        client = _client(args.provider)
        sample_client = _client(args.provider, args.sample_temperature) if args.samples else None
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

    if args.only:
        needles = [n.strip() for n in args.only.split(",") if n.strip()]
        items = [it for it in items if any(n in it[0].case_id or n in it[0].tags for n in needles)]
    if args.sample_cases and args.sample_cases < len(items):
        import random

        # Keep source order (grouped by database) so prompts share a cached prefix.
        keep = set(random.Random(args.seed).sample(range(len(items)), args.sample_cases))
        items = [it for i, it in enumerate(items) if i in keep]
    if args.limit:
        items = items[: args.limit]
    case_delay = args.case_delay
    if case_delay is None:
        case_delay = 1.5 if provider == "groq" else 0.0

    from secure_query.planner import plan_question
    from secure_query.planner.sql_plan import plan_sql_question

    planner = plan_sql_question if args.planner == "sql" else plan_question
    print(f"planner={args.planner}")

    def run_one(item):
        case, catalog, db_path = item
        if case_delay > 0:
            time.sleep(case_delay)
        return run_live_case(
            case, catalog, client, db_path,
            repeats=args.repeat, samples=args.samples, sample_client=sample_client,
            planner=planner,
        )

    from concurrent.futures import ThreadPoolExecutor

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for result in pool.map(run_one, items):  # input order, so output is stable
            results.append(result)
            mark = _MARKS.get(result.verdict, result.verdict)
            consistency = ""
            if args.repeat > 1:
                # Refusals produce no plan, so there is no shape to agree on.
                consistency = (
                    f" [{result.consistency:.0%} stable]" if result.consistency else " [no plan]"
                )
            print(f"  [{mark:7}] {result.case.case_id}{consistency}", flush=True)
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
        print(
            "refusals by control   (correct = should decline; on answerable cases the "
            "blocked plan stopped a wrong / cost a right answer; false = no plan to score)"
        )
        for code, counts in stats["by_refusal_code"].items():
            print(
                f"  {code:20} correct={counts.get('correct', 0):3}"
                f"  stopped-wrong={counts.get('blocked_wrong', 0):3}"
                f"  cost-right={counts.get('blocked_right', 0):3}"
                f"  false={counts.get('false', 0):3}"
            )
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


SUITES_DIR = Path(__file__).resolve().parent / "suites"


def _load_domain_suite(name_or_path: str) -> tuple[Catalog, Path, list]:
    """Catalog, database and cases for a suite: a name under suites/ or a cases.json path.

    The suite file's "catalog" is relative to it; its "database" to the data dir.
    """
    from secure_query.demo import DATA_DIR
    from secure_query.evals.accuracy import load_live_suite

    suite_path = Path(name_or_path)
    if not suite_path.suffix:
        suite_path = SUITES_DIR / name_or_path / "cases.json"
    meta = json.loads(suite_path.read_text(encoding="utf-8"))
    catalog = Catalog.from_json_file(suite_path.parent / meta["catalog"])
    return catalog, DATA_DIR / meta["database"], load_live_suite(suite_path)


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
                "blocked": r.attempts[-1].blocked if r.attempts else None,
                "blocked_sql": r.attempts[-1].blocked_sql if r.attempts else None,
                "fingerprint": r.attempts[-1].fingerprint if r.attempts else None,
                "samples": r.samples,
                "sql": r.attempts[-1].sql if r.attempts else None,
            }
            for r in results
        ],
    }
    Path(path).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
