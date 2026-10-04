"""Eval domains other than Chinook, each a self-contained folder.

A domain is `domains/<name>/` with:
    catalog.json   approved catalog, as a data owner would ship it (no Python)
    cases.json     {"cases": [...]} in the live-suite format
    build_db.py    `build(path)` writing a deterministic DuckDB file, and `DB_PATH`

Domains exist to catch fixes that only work on Chinook. Treat their cases as
holdout: measure on them, never tune prompts, guards, or code against them.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from secure_query.evals.accuracy import LiveCase, load_live_suite
from secure_query.kernel.catalog import Catalog

DOMAINS_DIR = Path(__file__).resolve().parent


def available_domains() -> list[str]:
    return sorted(
        p.name for p in DOMAINS_DIR.iterdir() if p.is_dir() and (p / "catalog.json").exists()
    )


def load_domain(name: str) -> tuple[Catalog, Path, list[LiveCase]]:
    """Catalog, database path (built if missing), and eval cases for a domain."""
    folder = DOMAINS_DIR / name
    if not (folder / "catalog.json").exists():
        raise KeyError(f"unknown eval domain {name!r}; known: {available_domains()}")
    catalog = Catalog.model_validate_json((folder / "catalog.json").read_text(encoding="utf-8"))
    builder = importlib.import_module(f"secure_query.evals.domains.{name}.build_db")
    db_path = Path(builder.DB_PATH)
    if not db_path.exists():
        builder.build(db_path)
    return catalog, db_path, load_live_suite(folder / "cases.json")
