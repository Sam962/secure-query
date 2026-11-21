"""KaggleDBQA: eight real Kaggle databases with the documentation their owners published.

Lee, Polozov and Richardson, ACL 2021 (https://aclanthology.org/2021.acl-long.176/). The
questions and documentation are in github.com/Chia-Hsuan-Lee/KaggleDBQA (cloned to
data/kaggledbqa/repo); the SQLite databases come from the Google Drive folder that README
names (unzipped to data/kaggledbqa/databases). Splits: `fewshot` (87 questions, for tuning)
and `test` (185, held out like Spider test).

With `docs`, each catalog carries the owners' documentation the way a data owner would write
it in catalog review: column descriptions, what category codes mean (kept only when the codes
occur in the column, as a review should check), and the database overview.
Without it, the catalog has names and types only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from secure_query.demo import DATA_DIR
from secure_query.evals.accuracy import LiveCase
from secure_query.evals.bird import _gold_tags, catalog_from, sqlite_to_duckdb
from secure_query.kernel.catalog import Catalog

ROOT = DATA_DIR / "kaggledbqa"
MAX_CODES = 600  # characters of code meanings per column


@lru_cache(maxsize=None)
def _schemas() -> dict[str, dict]:
    tables = json.loads((ROOT / "repo" / "KaggleDBQA_tables.json").read_text(encoding="utf-8"))
    return {s["db_id"]: s for s in tables}


def _sqlite(db_id: str) -> Path:
    return ROOT / "databases" / db_id / f"{db_id}.sqlite"


def _codes_occur(duck: Path, table: str, column: str, codes: list[str]) -> bool:
    """Are at least half the column's distinct values documented codes? Wrong documentation is worse than none:
    KaggleDBQA's Pesticide docs give every code column the list of US states."""
    import duckdb

    con = duckdb.connect(str(duck), read_only=True)
    try:
        quoted_t, quoted_c = '"' + table.replace('"', '""') + '"', '"' + column.replace('"', '""') + '"'
        hit, distinct = con.execute(
            f"SELECT COUNT(DISTINCT {quoted_c}) FILTER (WHERE CAST({quoted_c} AS VARCHAR) IN (SELECT UNNEST(?))), "
            f"COUNT(DISTINCT {quoted_c}) FROM {quoted_t}",
            [codes],
        ).fetchone() or (0, 0)
        return distinct > 0 and 2 * hit >= distinct
    finally:
        con.close()


def _with_docs(catalog: Catalog, schema: dict, duck: Path) -> Catalog:
    tables_orig = schema["table_names_original"]
    enums = schema.get("value_enums") or {}
    notes: dict[tuple[str, str], str] = {}
    for (t, col), desc in zip(schema["column_names_original"], schema["column_descriptions"], strict=True):
        if t < 0:
            continue
        text = " ".join((desc or "").split())
        codes = enums.get(col) if isinstance(enums, dict) else None
        if isinstance(codes, dict) and codes and _codes_occur(duck, tables_orig[t], col, list(codes)):
            listed = "; ".join(f"{k} = {v}" for k, v in codes.items())
            text = f"{text} Codes: {listed[:MAX_CODES]}".strip()
        if text:
            notes[(tables_orig[t].lower(), col.lower())] = text
    tables = [
        t.model_copy(update={"columns": [
            c.model_copy(update={"description": notes.get((t.name.lower(), c.name.lower()), c.description)})
            for c in t.columns
        ]})
        for t in catalog.tables
    ]
    overview = " ".join((schema.get("db_overview") or "").split())
    update: dict = {"tables": tables}
    if overview:
        update["instructions"] = [*catalog.instructions, f"About this data: {overview}"]
    return catalog.model_copy(update=update)


def load_kaggledbqa(split: str = "test", *, docs: bool = False) -> list[tuple[LiveCase, Catalog, Path]]:
    """(case, catalog, database) triples. Reference SQL runs on the SQLite file, the system on DuckDB."""
    import sqlite3

    if split not in ("test", "fewshot"):
        raise ValueError("split is 'test' or 'fewshot'")
    out: list[tuple[LiveCase, Catalog, Path]] = []
    skipped = {"gold_error": 0, "gold_empty": 0}
    for db_id, schema in sorted(_schemas().items()):
        duck = sqlite_to_duckdb(_sqlite(db_id), ROOT / "duckdb" / f"{db_id}.duckdb")
        catalog = catalog_from(duck, schema, tenant_id=f"kaggledbqa_{db_id}")
        if docs:
            catalog = _with_docs(catalog, schema, duck)
        items = json.loads((ROOT / "repo" / "examples" / f"{db_id}_{split}.json").read_text(encoding="utf-8"))
        for n, item in enumerate(items):
            con = sqlite3.connect(f"file:{_sqlite(db_id).resolve()}?mode=ro&immutable=1", uri=True)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            try:
                rows = con.execute(item["query"]).fetchall()
            except sqlite3.Error:
                skipped["gold_error"] += 1
                continue
            finally:
                con.close()
            if not rows or (len(rows) == 1 and all(v in (None, 0) for v in rows[0])):
                skipped["gold_empty"] += 1
                continue
            out.append((
                LiveCase(
                    case_id=f"kaggledbqa_{split}_{db_id}_{n:03d}",
                    question=item["question"],
                    expect="answer",
                    reference_sql=item["query"],
                    reference_db=str(_sqlite(db_id)),
                    tags=(*_gold_tags(item["query"], "kaggledbqa"), f"db:{db_id}"),
                    extra_columns_ok=True,
                ),
                catalog,
                duck,
            ))
    print(f"kaggledbqa {split}{' +docs' if docs else ''}: {len(out)} cases; skipped {skipped}")
    return out
