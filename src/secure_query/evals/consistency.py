"""Offline self-consistency analysis of an eval log written with --samples K.

One logged run gives the whole trade-off curve, with no further model calls:

    baseline      answer with the temperature-0 plan (the product today)
    agree>=m      answer only if >= m of the K samples returned the same rows
    vote>=m       answer with the most common result among all K+1 runs,
                  if it has >= m votes

Rows are compared by fingerprint (order-insensitive hash), so two different
plans that return the same answer agree.

Usage:
    python -m secure_query.evals.consistency docs/baselines/spider-dev-sc3-2026-10-04.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from secure_query.evals.accuracy import wilson_interval


def _baseline(case: dict) -> str:
    return {"correct": "right", "wrong": "wrong", "unsafe": "wrong"}.get(case["verdict"], "refused")


def _agree(case: dict, m: int) -> str:
    base = _baseline(case)
    if base == "refused":
        return base
    same = sum(1 for s in case["samples"] if s.get("fingerprint") == case["fingerprint"])
    return base if same >= m else "refused"


def _vote(case: dict, m: int) -> str:
    runs = [(case["fingerprint"], _baseline(case))] if _baseline(case) != "refused" else []
    runs += [(s["fingerprint"], s["verdict"]) for s in case["samples"] if s.get("verdict")]
    if not runs:
        return "refused"
    fingerprint, votes = Counter(fp for fp, _ in runs).most_common(1)[0]
    if votes < m:
        return "refused"
    return next(v for fp, v in runs if fp == fingerprint)


def policies(k: int) -> dict[str, callable]:
    out = {"baseline": _baseline}
    for m in range(1, k + 1):
        out[f"agree>={m}"] = lambda c, m=m: _agree(c, m)
    for m in range(2, k + 2):
        out[f"vote>={m}"] = lambda c, m=m: _vote(c, m)
    return out


def score(cases: list[dict], policy) -> dict[str, float]:
    outcomes = Counter(policy(c) for c in cases)
    n, wrong, right = len(cases), outcomes["wrong"], outcomes["right"]
    answered = right + wrong
    return {
        "n": n,
        "right": right,
        "wrong": wrong,
        "answer_rate": right / n if n else 0.0,
        "wrong_rate": wrong / n if n else 0.0,
        "wrong_hi": wilson_interval(wrong, n)[1],
        "precision": right / answered if answered else 1.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    cases = json.loads(args.report.read_text(encoding="utf-8"))["cases"]
    k = max((len(c.get("samples") or []) for c in cases), default=0)
    if not k:
        print("report has no samples; run evals.run with --samples K")
        return 2
    groups = {"all": cases}
    for tag in ("simple", "nested", "setop"):
        subset = [c for c in cases if tag in c["tags"]]
        if subset:
            groups[tag] = subset
    for name, subset in groups.items():
        print(f"\n== {name} (n={len(subset)}, K={k})")
        print(f"  {'policy':10} {'answer':>7} {'wrong':>7} {'wrong 95% hi':>13} {'precision':>10}")
        for label, policy in policies(k).items():
            r = score(subset, policy)
            print(
                f"  {label:10} {r['answer_rate']:7.1%} {r['wrong_rate']:7.1%}"
                f" {r['wrong_hi']:13.1%} {r['precision']:10.1%}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
