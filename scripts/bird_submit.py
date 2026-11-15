"""Run the production SQL path on official BIRD dev and write predict_dev.json.

The file is the one BIRD's evaluator scores. Evidence is appended to the question
(oracle knowledge; say so if this file is submitted). The kernel emits DuckDB SQL;
that statement is transpiled to SQLite, which is what the official grader executes.
A refusal is an invalid statement, so the grader marks it wrong.

    python scripts/bird_submit.py --limit 3
    python scripts/bird_submit.py

Resume is automatic: each question is appended to progress.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from functools import partial
from pathlib import Path

import sqlglot

from secure_query.engine.env import load_dotenv
from secure_query.evals import bird
from secure_query.evals.accuracy import _answer_probe, _probe
from secure_query.evals.run import _client, _second_reader
from secure_query.planner.llm import PlannerError
from secure_query.planner.sql_plan import plan_sql_question

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "data" / "bird" / "official"
DATA = OFFICIAL / "unpacked"
PROGRESS = OFFICIAL / "progress.jsonl"
OUT_DIR = OFFICIAL / "submit"
REFUSAL_SQL = "SELECT this_query_was_refused"


def _sqlite_sql(duckdb_sql: str) -> str:
    return sqlglot.transpile(duckdb_sql, read="duckdb", write="sqlite")[0]


def _question(item: dict) -> str:
    evidence = (item.get("evidence") or "").strip()
    if not evidence:
        return item["question"]
    return f"{item['question']}\n\nHint: {evidence}"


def _load_done(path: Path) -> dict[int, dict]:
    done: dict[int, dict] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        done[int(record["index"])] = record
    return done


def _predict(done: dict[int, dict], total: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        str(i): f"{done[i]['sqlite_sql']}\t----- bird -----\t{done[i]['db_id']}"
        for i in range(total)
        if i in done
    }
    (OUT_DIR / "predict_dev.json").write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="BIRD official-dev predictions")
    parser.add_argument("--limit", type=int, default=None, help="Stop after the first N questions")
    parser.add_argument("--progress", type=Path, default=PROGRESS)
    parser.add_argument(
        "--indices-file",
        type=Path,
        default=None,
        help="Run only these question indexes, one per line",
    )
    args = parser.parse_args()

    dev = json.loads((OFFICIAL / "dev.json").read_text(encoding="utf-8"))
    total = len(dev) if args.limit is None else min(args.limit, len(dev))
    selected = None
    if args.indices_file is not None:
        selected = {
            int(line)
            for line in args.indices_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    bird.BIRD_DIR = OFFICIAL
    bird.BIRD_DATA = DATA
    bird._schemas.cache_clear()

    client = _client("openai")
    check = _second_reader(client, "openai")
    progress_path = args.progress
    done = _load_done(progress_path)
    catalogs: dict[str, object] = {}
    started = time.time()
    answered = refused = errors = 0

    with progress_path.open("a", encoding="utf-8") as progress:
        for index, item in enumerate(dev[:total]):
            if selected is not None and index not in selected:
                continue
            if index in done:
                continue
            db_id = item["db_id"]
            if db_id not in catalogs:
                print(f"catalog {db_id}", flush=True)
                catalogs[db_id] = bird.build_catalog(db_id)
            catalog = catalogs[db_id]
            db_path = bird.duckdb_copy(db_id)
            planner = partial(
                plan_sql_question,
                value_probe=partial(_probe, db_path),
                answer_probe=partial(_answer_probe, db_path),
                check_client=check,
            )
            status = "refused"
            sqlite_sql = REFUSAL_SQL
            detail = ""
            t0 = time.time()
            try:
                planned = planner(_question(item), catalog, client, max_repairs=1)
                if planned.status == "ok" and planned.compiled is not None:
                    sqlite_sql = _sqlite_sql(planned.compiled.sql)
                    status = "answered"
                else:
                    detail = planned.clarify_code or "refused"
            except (PlannerError, sqlglot.errors.SqlglotError, ValueError) as exc:
                status = "error"
                detail = f"{type(exc).__name__}: {exc}"
            record = {
                "index": index,
                "question_id": item["question_id"],
                "db_id": db_id,
                "difficulty": item.get("difficulty"),
                "status": status,
                "detail": detail,
                "sqlite_sql": sqlite_sql,
                "seconds": round(time.time() - t0, 2),
            }
            progress.write(json.dumps(record) + "\n")
            progress.flush()
            done[index] = record
            answered += status == "answered"
            refused += status == "refused"
            errors += status == "error"
            elapsed = time.time() - started
            finished = sum(1 for i in range(total) if i in done)
            print(
                f"{index + 1}/{total} {status} {db_id} {record['seconds']}s "
                f"answered={answered} refused={refused} errors={errors} "
                f"elapsed={elapsed / 60:.1f}m",
                flush=True,
            )

    if selected is None:
        _predict(done, total)
        print(f"wrote {OUT_DIR / 'predict_dev.json'} ({total} questions)")
    else:
        print(f"wrote {progress_path} ({len(done)} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
