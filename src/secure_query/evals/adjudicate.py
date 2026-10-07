"""Adjudicate disputed Spider verdicts: is a mismatch a real error?

Exact-match scoring counts as wrong both real mistakes and differences that
are not mistakes (an ambiguous question read another valid way, a faulty gold
query, an engine artifact). This builds a packet per disputed case and asks an
independent model rater to label it with a fixed rubric. A human rater labels
the same packets blind; the merge is conservative (real_error if either says so).

Usage:
    python -m secure_query.evals.adjudicate packets REPORT.json [BLOCKED.json] -o packets.json
    python -m secure_query.evals.adjudicate rate packets.json -o model_labels.json --model gpt-5-2025-08-07
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import duckdb

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

Judge only against the question and schema. When unsure between real_error and
another label, choose real_error. Reply with JSON only:
{"label": "real_error" | "ambiguous" | "benchmark_fault", "reason": "<one sentence>"}"""


def _rows(rows: list, limit: int = 8) -> dict:
    return {"count": len(rows), "first": [list(map(str, r)) for r in rows[:limit]]}


def build_packets(report: Path, blocked: Path | None) -> list[dict]:
    items = {c.case_id: (c, cat, db) for c, cat, db in load_spider("dev")}
    disputed = [
        (c, c["sql"], "answered_wrong")
        for c in json.loads(report.read_text())["cases"]
        if c["verdict"] == "wrong" and c["sql"]
    ]
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


def rate(packets: list[dict], model: str) -> dict[str, dict]:
    from openai import OpenAI

    client = OpenAI()
    out: dict[str, dict] = {}
    for p in packets:
        body = {k: p[k] for k in ("question", "schema", "gold_sql", "gold_result", "system_sql", "system_result")}
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RUBRIC},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        label = json.loads(resp.choices[0].message.content)
        if label.get("label") not in LABELS:
            label = {"label": "real_error", "reason": f"unparseable rating: {label}"}
        out[p["id"] + ":" + p["kind"]] = label
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("packets")
    pk.add_argument("report", type=Path)
    pk.add_argument("blocked", type=Path, nargs="?")
    pk.add_argument("-o", "--output", type=Path, required=True)
    rt = sub.add_parser("rate")
    rt.add_argument("packets", type=Path)
    rt.add_argument("-o", "--output", type=Path, required=True)
    rt.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "packets":
        packets = build_packets(args.report, args.blocked)
        args.output.write_text(json.dumps(packets, indent=1, ensure_ascii=False))
        print(f"{len(packets)} packets -> {args.output}")
    else:
        labels = rate(json.loads(args.packets.read_text()), args.model)
        args.output.write_text(json.dumps(labels, indent=1, ensure_ascii=False))
        print(f"{len(labels)} labels -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
