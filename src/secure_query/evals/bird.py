"""BIRD Mini-Dev benchmark adapter: real-world, domain-knowledge-heavy databases.

BIRD (A BIg Bench for Large-scale Database Grounded Text-to-SQL) is harder than
Spider: questions come with an "evidence" hint (e.g. "price refers to unit_price
column"), analogous to the catalog's metric/description annotations, which is
appended to the question so the LLM sees it.

Source: https://bird-bench.github.io / https://github.com/bird-bench/mini_dev
Data:   https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip

500 questions, 11 databases, SQLite.  Distribution: simple 30% / moderate 50%
/ challenging 20%.  Cases with gold errors or empty results are skipped.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from functools import lru_cache
from pathlib import Path

import sqlglot
from sqlglot import exp

from secure_query.demo import DATA_DIR
from secure_query.evals.accuracy import LiveCase
from secure_query.evals.spider import _column_type, _coerce, _DUCK_TYPES
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec

BIRD_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
BIRD_SHA256 = "cc48ba16838204e4e214512030cb572eeb5f7bcdd999bae4b9b6ff12ec13b92f"

BIRD_DIR = DATA_DIR / "bird"
# ZIP extracts to minidev/MINIDEV/
BIRD_DATA = BIRD_DIR / "minidev" / "MINIDEV"

_QUESTIONS_FILE = "mini_dev_sqlite.json"
_TABLES_FILE = "dev_tables.json"
_DB_ROOT = "dev_databases"


def download_bird() -> Path:
    """Download the BIRD mini-dev ZIP if not present; verify SHA256 and extract."""
    archive = BIRD_DIR / "minidev.zip"
    BIRD_DIR.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        import urllib.request

        print(f"Downloading BIRD mini-dev → {archive}  (~800 MB, first time only)")
        urllib.request.urlretrieve(BIRD_URL, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != BIRD_SHA256:
        raise ValueError(
            f"{archive} sha256 {digest[:12]} != pinned {BIRD_SHA256[:12]}"
        )
    if not BIRD_DATA.exists():
        print("Extracting BIRD mini-dev…")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(BIRD_DIR)
    return BIRD_DATA


def duckdb_copy(db_id: str) -> Path:
    """DuckDB copy of one BIRD SQLite database, built once."""
    import sqlite3

    import duckdb

    out = BIRD_DIR / "duckdb" / f"{db_id}.duckdb"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    sqlite_path = BIRD_DATA / _DB_ROOT / db_id / f"{db_id}.sqlite"
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.text_factory = lambda b: b.decode("utf-8", "replace")
    duck = duckdb.connect(str(tmp))
    try:
        tables = [
            r[0]
            for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            cur = source.execute(f"SELECT * FROM {quoted}")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            types = [_column_type([r[i] for r in rows]) for i in range(len(columns))]
            ddl = ", ".join(
                '"' + c.replace('"', '""') + f'" {t}'
                for c, t in zip(columns, types, strict=True)
            )
            duck.execute(f"CREATE TABLE {quoted} ({ddl})")
            if rows:
                marks = ", ".join("?" * len(columns))
                duck.executemany(
                    f"INSERT INTO {quoted} VALUES ({marks})",
                    [_coerce(r, types) for r in rows],
                )
    finally:
        duck.close()
        source.close()
    tmp.rename(out)
    return out


@lru_cache(maxsize=None)
def _schemas() -> dict[str, dict]:
    path = BIRD_DATA / _TABLES_FILE
    return {s["db_id"]: s for s in json.loads(path.read_text(encoding="utf-8"))}


def build_catalog(db_id: str) -> Catalog:
    """Catalog from DuckDB real types plus dev_tables.json foreign keys."""
    import duckdb

    con = duckdb.connect(str(duckdb_copy(db_id)), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall()
    finally:
        con.close()

    actual = {(t.lower(), c.lower()): (t, c, d) for t, c, d in rows}
    by_table: dict[str, list[ColumnSpec]] = {}
    names: dict[str, str] = {}
    for (tkey, _ckey), (table, column, dtype) in actual.items():
        names[tkey] = table
        by_table.setdefault(table, []).append(
            ColumnSpec(
                name=column,
                dtype=_DUCK_TYPES.get(dtype.split("(")[0].upper(), "str"),  # type: ignore[arg-type]
            )
        )

    schema = _schemas().get(db_id, {})
    tables_orig = schema.get("table_names_original", [])
    col_refs = schema.get("column_names_original", [])
    join_keys: list[JoinKey] = []
    for fk, ref in schema.get("foreign_keys", []):
        (lt, lc), (rt, rc) = col_refs[fk], col_refs[ref]
        left = actual.get((tables_orig[lt].lower(), lc.lower()))
        right = actual.get((tables_orig[rt].lower(), rc.lower()))
        if left is None or right is None or left[0] == right[0]:
            continue
        key = JoinKey(
            left_table=left[0], left_column=left[1],
            right_table=right[0], right_column=right[1],
        )
        if key not in join_keys:
            join_keys.append(key)

    return Catalog(
        tenant_id=f"bird_{db_id}",
        tables=[TableSpec(name=t, columns=cols) for t, cols in by_table.items()],
        join_keys=join_keys,
        max_limit=10_000,
    )


def _gold_tags(sql: str, difficulty: str) -> tuple[str, ...]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return (difficulty,)
    tags: list[str] = [difficulty]
    if any(isinstance(n, (exp.Union, exp.Intersect, exp.Except)) for n in tree.walk()):
        tags.append("setop")
    if any(sel is not tree for sel in tree.find_all(exp.Select)):
        tags.append("nested")
    if any(isinstance(n, (exp.Add, exp.Sub, exp.Mul, exp.Div)) for n in tree.walk()):
        tags.append("arith")
    if tree.find(exp.Join):
        tags.append("join")
    return tuple(tags)


@lru_cache(maxsize=None)
def _items() -> list[dict]:
    return json.loads((BIRD_DATA / _QUESTIONS_FILE).read_text(encoding="utf-8"))


def load_bird_mini_dev(
    *, limit: int | None = None, difficulty: str | None = None
) -> list[tuple[LiveCase, Catalog, Path]]:
    """(case, catalog, database) triples for the BIRD mini-dev SQLite split.

    `difficulty`: filter to "simple", "moderate", or "challenging" (default: all).
    Gold SQL runs on the original SQLite file; the system runs on the DuckDB copy.
    The evidence hint is appended to the question so the LLM sees it.
    """
    import sqlite3

    download_bird()
    items = _items()
    catalogs: dict[str, Catalog] = {}
    out: list[tuple[LiveCase, Catalog, Path]] = []
    skipped = {"gold_error": 0, "gold_empty": 0, "difficulty_filter": 0}

    for item in items:
        db_id = item["db_id"]
        diff = item.get("difficulty", "simple")

        if difficulty and diff != difficulty:
            skipped["difficulty_filter"] += 1
            continue

        sqlite_path = BIRD_DATA / _DB_ROOT / db_id / f"{db_id}.sqlite"
        con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            rows = con.execute(item["SQL"]).fetchall()
        except sqlite3.Error:
            skipped["gold_error"] += 1
            continue
        finally:
            con.close()

        if not rows or (len(rows) == 1 and all(v in (None, 0) for v in rows[0])):
            skipped["gold_empty"] += 1
            continue

        if db_id not in catalogs:
            catalogs[db_id] = build_catalog(db_id)

        evidence = (item.get("evidence") or "").strip()
        question = item["question"]
        if evidence:
            question = f"{question}\n\nHint: {evidence}"

        case = LiveCase(
            case_id=f"bird_{item['question_id']:04d}_{db_id}",
            question=question,
            expect="answer",
            reference_sql=item["SQL"],
            reference_db=str(sqlite_path),
            ordered=False,
            tags=(*_gold_tags(item["SQL"], diff), f"db:{db_id}"),
            extra_columns_ok=True,
        )
        out.append((case, catalogs[db_id], duckdb_copy(db_id)))
        if limit and len(out) >= limit:
            break

    print(
        f"bird mini-dev: {len(out)} cases from {len(catalogs)} databases; "
        f"skipped {skipped}"
    )
    return out
