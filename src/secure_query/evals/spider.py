"""Spider benchmark adapter: many schemas the system was never tuned on.

Everything is generated from the pinned Spider release, with no hand curation:
  - one DuckDB copy per SQLite database (the product's execute engine),
  - one catalog per database: actual column types, Spider's foreign keys as
    join keys, Spider's natural-language column names as descriptions,
  - gold SQL transpiled SQLite → DuckDB and tagged by what it needs
    (nested, setop, arith); "simple" gold is what the LogicalPlan can express.

Scoring differences from the official Spider metric, both deliberate:
  - column containment (`extra_columns_ok`): a list plan returns the whole row
    where gold selects one column; the gold columns must appear, same rows;
  - cases whose gold result is empty are dropped: an empty answer would match
    by accident.

Spider has no questions that should be refused, so it measures wrong-rate and
answer-rate, not refusal quality. Dev is the measurement set; test is held out.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
import zipfile
from functools import lru_cache
from pathlib import Path

import sqlglot
from sqlglot import exp

from secure_query.demo import DATA_DIR
from secure_query.evals.accuracy import LiveCase
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec

SPIDER_COMMIT = "4a01bbac6520cd35b216db9e1724e5e1ada60aa4"
SPIDER_SHA256 = "00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b"
SPIDER_URL = (
    "https://huggingface.co/datasets/HAL-9001/spider-databases/resolve/"
    f"{SPIDER_COMMIT}/spider_data.zip"
)
SPIDER_DIR = DATA_DIR / "spider"
SPIDER_DATA = SPIDER_DIR / "spider_data"
SPLITS = {
    "dev": ("dev.json", "tables.json", "database"),
    "test": ("test.json", "test_tables.json", "test_database"),
}
_DUCK_TYPES = {
    "BIGINT": "int", "INTEGER": "int", "SMALLINT": "int", "TINYINT": "int", "HUGEINT": "int",
    "DOUBLE": "float", "FLOAT": "float", "REAL": "float", "DECIMAL": "float",
    "BOOLEAN": "bool", "DATE": "datetime", "TIMESTAMP": "datetime",
}


def download_spider() -> Path:
    archive = SPIDER_DIR / "spider_data.zip"
    SPIDER_DIR.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        print(f"Downloading Spider → {archive}")
        urllib.request.urlretrieve(SPIDER_URL, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != SPIDER_SHA256:
        raise ValueError(f"{archive} sha256 {digest[:12]} != pinned {SPIDER_SHA256[:12]}")
    if not SPIDER_DATA.exists():
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(SPIDER_DIR)
    return SPIDER_DATA


def duckdb_copy(split: str, db_id: str) -> Path:
    """DuckDB copy of one Spider SQLite database, built once.

    Spider data is dirty (empty strings in date columns, invalid UTF-8), so rows
    are read with sqlite3 and each column is typed by its values: BIGINT or
    DOUBLE only when every value is numeric, else VARCHAR (SQLite's own
    comparison semantics for mixed columns).
    """
    import sqlite3

    import duckdb

    out = SPIDER_DIR / "duckdb" / split / f"{db_id}.duckdb"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(SPIDER_DATA / SPLITS[split][2] / db_id / f"{db_id}.sqlite")
    source.text_factory = lambda b: b.decode("utf-8", "replace")
    duck = duckdb.connect(str(tmp))
    try:
        tables = [r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            cur = source.execute(f"SELECT * FROM {quoted}")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            types = [_column_type([r[i] for r in rows]) for i in range(len(columns))]
            ddl = ", ".join(
                '"' + c.replace('"', '""') + f'" {t}' for c, t in zip(columns, types, strict=True)
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


def _column_type(values: list) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return "BIGINT"
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "DOUBLE"
    return "VARCHAR"


def _coerce(row: tuple, types: list[str]) -> tuple:
    return tuple(
        None if v is None else (str(v) if t == "VARCHAR" else v)
        for v, t in zip(row, types, strict=True)
    )


def build_catalog(split: str, schema: dict) -> Catalog:
    """Catalog from the DuckDB copy's real types plus Spider's foreign keys."""
    import duckdb

    db_id = schema["db_id"]
    con = duckdb.connect(str(duckdb_copy(split, db_id)), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall()
    finally:
        con.close()
    actual = {(t.lower(), c.lower()): (t, c, d) for t, c, d in rows}

    tables = schema["table_names_original"]
    described = {
        (tables[t].lower(), orig.lower()): nl
        for (t, orig), (_, nl) in zip(schema["column_names_original"], schema["column_names"])
        if t >= 0
    }
    by_table: dict[str, list[ColumnSpec]] = {}
    names: dict[str, str] = {}
    for (tkey, ckey), (table, column, dtype) in actual.items():
        names[tkey] = table
        nl = described.get((tkey, ckey), "")
        by_table.setdefault(table, []).append(
            ColumnSpec(
                name=column,
                dtype=_DUCK_TYPES.get(dtype.split("(")[0].upper(), "str"),  # type: ignore[arg-type]
                description=nl if nl and nl != column.lower() else "",
            )
        )

    col_refs = [(t, c) for t, c in schema["column_names_original"]]
    join_keys: list[JoinKey] = []
    for fk, ref in schema["foreign_keys"]:
        (lt, lc), (rt, rc) = col_refs[fk], col_refs[ref]
        left = actual.get((tables[lt].lower(), lc.lower()))
        right = actual.get((tables[rt].lower(), rc.lower()))
        if left is None or right is None or left[0] == right[0]:
            continue  # unknown column or self-reference (not expressible as a join)
        key = JoinKey(left_table=left[0], left_column=left[1], right_table=right[0], right_column=right[1])
        if key not in join_keys:
            join_keys.append(key)

    return Catalog(
        tenant_id=f"spider_{db_id}",
        tables=[TableSpec(name=t, columns=cols) for t, cols in by_table.items()],
        join_keys=join_keys,
        max_limit=10_000,  # benchmark gold can return thousands of rows
    )


def gold_tags(sql: str) -> tuple[str, ...]:
    """What the gold query needs; "simple" means the LogicalPlan can express it."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    tags: list[str] = []
    if any(isinstance(node, (exp.Union, exp.Intersect, exp.Except)) for node in tree.walk()):
        tags.append("setop")
    if any(sel is not tree for sel in tree.find_all(exp.Select)):
        tags.append("nested")
    if any(isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)) for node in tree.walk()):
        tags.append("arith")
    if tree.find(exp.Join):
        tags.append("join")
    return tuple(tags) if set(tags) - {"join"} else (*tags, "simple")


@lru_cache(maxsize=None)
def _schemas(split: str) -> dict[str, dict]:
    path = SPIDER_DATA / SPLITS[split][1]
    return {s["db_id"]: s for s in json.loads(path.read_text(encoding="utf-8"))}


def load_spider(split: str, *, limit: int | None = None) -> list[tuple[LiveCase, Catalog, Path]]:
    """(case, catalog, database) triples for a Spider split.

    Gold SQL runs on the original SQLite file (the engine it was written for);
    the system's SQL runs on the DuckDB copy.
    """
    import sqlite3

    download_spider()
    raw = json.loads((SPIDER_DATA / SPLITS[split][0]).read_text(encoding="utf-8"))
    catalogs: dict[str, Catalog] = {}
    out: list[tuple[LiveCase, Catalog, Path]] = []
    skipped = {"gold_error": 0, "gold_empty": 0}
    for i, item in enumerate(raw):
        db_id = item["db_id"]
        if db_id not in catalogs:
            catalogs[db_id] = build_catalog(split, _schemas(split)[db_id])
        sqlite_path = SPIDER_DATA / SPLITS[split][2] / db_id / f"{db_id}.sqlite"
        con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
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
        case = LiveCase(
            case_id=f"{split}_{i:04d}_{db_id}",
            question=item["question"],
            expect="answer",
            reference_sql=item["query"],
            reference_db=str(sqlite_path),
            # Row sets: tied ORDER BY keys make strict order arbitrary. Top-N is
            # still checked through the LIMIT tie logic.
            ordered=False,
            tags=(*gold_tags(item["query"]), f"db:{db_id}"),
            extra_columns_ok=True,
        )
        out.append((case, catalogs[db_id], duckdb_copy(split, db_id)))
        if limit and len(out) >= limit:
            break
    print(f"spider {split}: {len(out)} cases from {len(catalogs)} databases; skipped {skipped}")
    return out
