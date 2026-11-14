"""Adjudicate disputed Spider verdicts: is a mismatch a real error?

Exact-match scoring counts as wrong both real mistakes and differences that
are not mistakes (an ambiguous question read another valid way, a faulty gold
query, an engine artifact). This builds a packet per disputed case and asks an
independent model rater to label it with a fixed rubric. A human rater labels
the same packets blind; the merge is conservative (real_error if either says so).

Usage:
    python scripts/adjudicate.py packets REPORT.json [BLOCKED.json] -o packets.json
    python scripts/adjudicate.py packets SCORECARD.json -o packets.json   # head-to-head, blind
    python scripts/adjudicate.py rate packets.json -o model_labels.json --model gpt-5-2025-08-07
    python scripts/adjudicate.py packets REPORT.json --right 60 -o right-packets.json   # audit matches

A result that matches the reference on one database can still come from a query that is
wrong on other data. `--right N` samples N matching answers (fixed seed) so a rater can
check the query itself; those packets are rated with RIGHT_RUBRIC.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from secure_query.engine.env import load_dotenv
from secure_query.evals.spider import load_spider

LABELS = ("real_error", "ambiguous", "benchmark_fault")

RUBRIC = """You adjudicate a text-to-SQL evaluation. A system answered a question with SQL;
its result differs from the benchmark's gold result. Decide which label applies:

- real_error: a careful analyst would call the system's answer wrong for this
  question. A user relying on it would be misled.
- ambiguous: the question genuinely allows the system's reading, and the system's
  answer is correct under that reading (e.g. including zero-count groups, LIKE vs =
  for a loosely named value, DISTINCT vs duplicates when the question is unclear).
- benchmark_fault: the gold query is wrong for the question, or the difference is an
  artifact (engine type coercion, case-insensitive LIKE in the gold engine,
  formatting), and the system's answer is right.

Ties: a single returned row is ambiguous only when the question asks for one and
that row is truly tied for the maximum (or minimum). When the question asks for
every row up to a cutoff (e.g. "top 3") and the answer drops one side of a tie at
the cutoff, label real_error.

Judge only against the question and schema. When unsure between real_error and
another label, choose real_error. Reply with JSON only:
{"label": "real_error" | "ambiguous" | "benchmark_fault", "reason": "<one sentence>"}"""


RIGHT_LABELS = ("sound", "hidden_error")

RIGHT_RUBRIC = """You audit a text-to-SQL evaluation. A system answered a question with SQL and
its result matches the benchmark's gold result on the one database tested. A match on one
database does not prove the query is right. Read the system SQL against the question and
the schema and decide:

- sound: the system SQL answers the question on any reasonable data in this schema.
  Differences from the gold SQL that cannot change the answer the question asks for do
  not matter, and neither does a defensible reading of an unclear question.
- hidden_error: there is plausible data in this schema on which a careful analyst would
  call the system's answer wrong for this question (for example a missing DISTINCT, a
  join that multiplies rows, a filter on the wrong column, a dropped tie). This applies
  even when the gold SQL has the same fault.

Not errors: the trailing LIMIT 10000 (the product's row cap, applied to every answer);
one row for a question that asks for one, when rows could tie; leaving out groups with a
zero count when the question does not ask for them.

Label hidden_error only when you can name the data condition that exposes it. Reply with
JSON only:
{"label": "sound" | "hidden_error", "reason": "<one sentence; for hidden_error, the data condition>"}"""


def _rows(rows: list, limit: int = 8) -> dict:
    return {"count": len(rows), "first": [list(map(str, r)) for r in rows[:limit]]}


def build_packets(report: Path, blocked: Path | None, split: str = "dev", right: int = 0) -> list[dict]:
    """Packets for the answers scored wrong, or with `right` > 0 for a seeded sample of
    that many answers scored right."""
    import random

    items = {c.case_id: (c, cat, db) for c, cat, db in load_spider(split)}
    cases = json.loads(report.read_text())["cases"]
    if right:
        matched = [c for c in cases if c["verdict"] == "correct" and c["sql"]]
        sample = random.Random(0).sample(matched, min(right, len(matched)))
        disputed = [(c, c["sql"], "answered_right") for c in sample]
    else:
        disputed = [(c, c["sql"], "answered_wrong") for c in cases if c["verdict"] == "wrong" and c["sql"]]
    if blocked:
        disputed += [
            (c, c["blocked_sql"], f"blocked_{c['blocked']}")
            for c in json.loads(blocked.read_text())["cases"]
            if c.get("blocked") in ("right", "wrong") and c.get("blocked_sql")
        ]
    packets = []
    for c, sql, kind in disputed:
        case, catalog, db_path = items[c["id"]]
        gold = sqlite3.connect(f"file:{case.reference_db}?mode=ro", uri=True)
        gold.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            gold_rows = gold.execute(case.reference_sql).fetchall()
        finally:
            gold.close()
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            ours = con.execute(sql).fetchall()
        finally:
            con.close()
        packets.append(
            {
                "id": c["id"],
                "kind": kind,
                "guard": c.get("detail") if kind.startswith("blocked") else None,
                "question": case.question,
                "schema": catalog.planner_summary(),
                "gold_sql": case.reference_sql,
                "gold_result": _rows(gold_rows),
                "system_sql": sql,
                "system_result": _rows(ours),
            }
        )
    return packets


def build_blind_packets(scorecard: dict) -> tuple[list[dict], dict[str, dict]]:
    """Packets for every wrong answer of both systems in a scorecard file, and their key.

    A rater must not know which system wrote an answer. Packets are shuffled (fixed seed),
    numbered, and carry no system name or case id; the key maps each number back.
    """
    import random

    found = [
        (system, case)
        for system, body in sorted(scorecard["systems"].items())
        for case in body["cases"]
        if case["verdict"] == "wrong" and case.get("adjudication")
    ]
    random.Random(0).shuffle(found)
    packets, key = [], {}
    for number, (system, case) in enumerate(found, start=1):
        packet_id = f"p{number:03d}"
        evidence = case["adjudication"]
        packets.append(
            {
                "id": packet_id,
                "kind": "answered_wrong",
                "guard": None,
                "question": evidence["question"],
                "schema": scorecard.get("schema", ""),
                "gold_sql": evidence["reference_sql"],
                "gold_result": evidence["reference_result"],
                "system_sql": case.get("sql"),
                "system_result": evidence["system_result"],
            }
        )
        key[packet_id] = {"system": system, "case_id": case["id"]}
    return packets, key


def rate(packets: list[dict], model: str) -> dict[str, dict]:
    from openai import OpenAI

    client = OpenAI()
    out: dict[str, dict] = {}
    for p in packets:
        body = {k: p[k] for k in ("question", "schema", "gold_sql", "gold_result", "system_sql", "system_result")}
        audit = p["kind"] == "answered_right"
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RIGHT_RUBRIC if audit else RUBRIC},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        label = json.loads(resp.choices[0].message.content)
        if label.get("label") not in (RIGHT_LABELS if audit else LABELS):
            fallback = "hidden_error" if audit else "real_error"
            label = {"label": fallback, "reason": f"unparseable rating: {label}"}
        out[p["id"] + ":" + p["kind"]] = label
    return out


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("packets")
    pk.add_argument("report", type=Path)
    pk.add_argument("blocked", type=Path, nargs="?")
    pk.add_argument("-o", "--output", type=Path, required=True)
    pk.add_argument("--split", default="dev", choices=("dev", "test"))
    pk.add_argument("--right", type=int, default=0, metavar="N", help="sample N answers scored right instead")
    rt = sub.add_parser("rate")
    rt.add_argument("packets", type=Path)
    rt.add_argument("-o", "--output", type=Path, required=True)
    rt.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "packets":
        report = json.loads(args.report.read_text())
        if "systems" in report:  # a scorecard export: both systems, blind
            packets, key = build_blind_packets(report)
            key_path = args.output.with_suffix(".key.json")
            key_path.write_text(json.dumps(key, indent=1))
            print(f"key (do not give to raters) -> {key_path}")
        else:
            packets = build_packets(args.report, args.blocked, args.split, args.right)
        args.output.write_text(json.dumps(packets, indent=1, ensure_ascii=False, default=str))
        print(f"{len(packets)} packets -> {args.output}")
    else:
        labels = rate(json.loads(args.packets.read_text()), args.model)
        args.output.write_text(json.dumps(labels, indent=1, ensure_ascii=False))
        print(f"{len(labels)} labels -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
