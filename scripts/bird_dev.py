"""BIRD dev (1,534 questions) through the SQL planner and the two-readings gate, graded per question.

    PYTHONPATH=src python scripts/bird_dev.py out.jsonl [--facts] [--no-hints] [--descriptions]

The catalog for each database is drafted the way an owner would get it: BIRD's declared joins
plus the joins `with_discovered_joins` proposes. --facts adds `with_detected_facts`;
--descriptions adds BIRD's own column descriptions (database_description CSVs) as an owner's
glossary; --no-hints drops BIRD's per-question hint ("evidence"). An answer is right when its
rows equal the reference SQL's rows on the official SQLite file, compared as sets with floats
rounded to 4 places; the system SQL runs on DuckDB (SQLite has no YEAR() and the like), with
the SQLite translation tried as well. Every query has a time limit. Tune on this, never on
holdout or test data.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import duckdb
import sqlglot

from secure_query.engine.catalog_draft import with_detected_facts, with_discovered_joins
from secure_query.engine.env import load_dotenv
from secure_query.evals import bird
from secure_query.evals.accuracy import _answer_probe, _probe
from secure_query.evals.run import _client, _second_reader
from secure_query.kernel.catalog import Catalog
from secure_query.planner.sql_plan import plan_sql_question

OFFICIAL = Path("data/bird/official")
DBS = OFFICIAL / "unpacked" / "dev_databases"
LIMIT_SECONDS = 60


def descriptions(db: str, catalog: Catalog) -> Catalog:
    folder = DBS / db / "database_description"
    tables = []
    for t in catalog.tables:
        f = next((p for p in folder.glob("*.csv") if p.stem.lower() == t.name.lower()), None)
        notes: dict[str, str] = {}
        if f is not None:
            raw = f.read_bytes()
            text = raw.decode("utf-8-sig" if raw[:3] == b"\xef\xbb\xbf" else "utf-8", "replace")
            for row in csv.DictReader(io.StringIO(text)):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                name = row.get("original_column_name", "")
                seen: list[str] = []
                for part in (row.get("column_name"), row.get("column_description"), row.get("value_description")):
                    part = " ".join((part or "").split())
                    if part and part.lower() != name.lower() and part not in seen:
                        seen.append(part)
                if name and seen:
                    notes[name.lower()] = "; ".join(seen)[:300]
        cols = [c.model_copy(update={"description": notes.get(c.name.lower(), c.description)}) for c in t.columns]
        tables.append(t.model_copy(update={"columns": cols}))
    return catalog.model_copy(update={"tables": tables})


def _norm(rows) -> Counter:
    return Counter(tuple(round(v, 4) if isinstance(v, float) else v for v in r) for r in rows)


def _sqlite_rows(db: str, sql: str) -> list:
    con = sqlite3.connect(f"file:{DBS}/{db}/{db}.sqlite?mode=ro&immutable=1", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    deadline = time.monotonic() + LIMIT_SECONDS
    con.set_progress_handler(lambda: time.monotonic() > deadline, 100_000)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _duckdb_rows(path: Path, sql: str) -> list:
    con = duckdb.connect(str(path), read_only=True)
    timer = threading.Timer(LIMIT_SECONDS, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out")
    parser.add_argument("--facts", action="store_true")
    parser.add_argument("--no-hints", action="store_true")
    parser.add_argument("--descriptions", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    load_dotenv()
    bird.BIRD_DIR = OFFICIAL
    bird.BIRD_DATA = OFFICIAL / "unpacked"
    bird._schemas.cache_clear()
    dev = json.loads((OFFICIAL / "dev.json").read_text())

    catalogs: dict[str, Catalog] = {}
    for db in sorted({x["db_id"] for x in dev}):
        con = duckdb.connect(str(bird.duckdb_copy(db)), read_only=True)
        try:
            def run(sql: str, con=con):  # noqa: ANN001
                return con.execute(sql).fetchall()

            catalog = with_discovered_joins(bird.build_catalog(db), run)
            if args.facts:
                catalog = with_detected_facts(catalog, run)
        finally:
            con.close()
        catalogs[db] = descriptions(db, catalog) if args.descriptions else catalog
    client = _client("openai")
    check = _second_reader(client, "openai")

    def one(i: int) -> dict:
        item = dev[i]
        db = item["db_id"]
        path = bird.duckdb_copy(db)
        hint = item.get("evidence") and not args.no_hints
        question = item["question"] + (f"\n\nHint: {item['evidence']}" if hint else "")
        try:
            plan = plan_sql_question(question, catalogs[db], client, value_probe=partial(_probe, path),
                                     answer_probe=partial(_answer_probe, path), check_client=check)
        except Exception as e:  # noqa: BLE001
            return {"index": i, "db": db, "status": "error", "why": str(e)[:200]}
        if plan.status != "ok" or plan.compiled is None:
            return {"index": i, "db": db, "status": "refused", "code": plan.clarify_code}
        sql = plan.compiled.sql
        right = False
        try:
            gold = _norm(_sqlite_rows(db, item["SQL"]))
            try:
                right = _norm(_duckdb_rows(path, sql)) == gold
            except Exception:  # noqa: BLE001
                pass
            if not right:
                right = _norm(_sqlite_rows(db, sqlglot.transpile(sql, read="duckdb", write="sqlite")[0])) == gold
        except Exception:  # noqa: BLE001
            pass
        return {"index": i, "db": db, "status": "right" if right else "wrong", "sql": sql}

    counts: Counter = Counter()
    with ThreadPoolExecutor(args.workers) as pool, open(args.out, "w") as out:
        for result in pool.map(one, range(len(dev))):
            counts[result["status"]] += 1
            out.write(json.dumps(result) + "\n")
            out.flush()
    print(json.dumps(dict(counts)))


if __name__ == "__main__":
    main()
