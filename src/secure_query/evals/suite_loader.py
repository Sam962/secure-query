"""Load dev / holdout / all eval suites with split discipline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from secure_query.evals.accuracy import LiveCase, parse_cases
from secure_query.evals.dev_expansion import HOLDOUT_CASE_IDS, extra_dev_cases

EVALS_DIR = Path(__file__).resolve().parent
LEGACY_SUITE_PATH = EVALS_DIR / "suites" / "chinook" / "cases.json"

SplitName = Literal["dev", "holdout", "all"]


def _load_legacy_cases() -> list[dict]:
    return json.loads(LEGACY_SUITE_PATH.read_text())["cases"]


def load_suite(split: SplitName = "all") -> list[LiveCase]:
    """Load eval cases for the requested split.

    - holdout: frozen 12 cases — never tune prompts/guards on these
    - dev: legacy non-holdout + programmatic expansion (100+ total)
    - all: dev + holdout
    """
    legacy = _load_legacy_cases()
    holdout_raw = [c for c in legacy if c["id"] in HOLDOUT_CASE_IDS]
    dev_base = [c for c in legacy if c["id"] not in HOLDOUT_CASE_IDS]
    dev_raw = dev_base + extra_dev_cases()

    if split == "holdout":
        return parse_cases(holdout_raw)
    if split == "dev":
        return parse_cases(dev_raw)
    if split == "all":
        return parse_cases(dev_raw + holdout_raw)
    raise ValueError(f"unknown split: {split!r}")


def suite_stats(split: SplitName = "all") -> dict[str, int]:
    cases = load_suite(split)
    return {
        "total": len(cases),
        "answer": sum(1 for c in cases if c.expect == "answer"),
        "abstain": sum(1 for c in cases if c.expect == "abstain"),
    }
