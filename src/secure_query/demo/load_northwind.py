"""Load the Northwind database into a local DuckDB file (second validation schema).

Northwind is not used for tuning. It checks that the planner, guards and prompt
work on a schema they were not written against; everything domain-specific
lives in `src/secure_query/evals/suites/northwind/catalog.json`.

The DuckDB file mirrors every source table. What the planner may see is decided
by the catalog, not here.

Usage:
    python -m secure_query.demo.load_northwind
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from secure_query.demo import DATA_DIR

# Pinned to an upstream commit: eval reference answers depend on the exact rows.
NORTHWIND_COMMIT = "4f56e7f5906dfd23b25244c5bfe8fb5da6402efd"
NORTHWIND_SHA256 = "2f4f5c68dfcd33ba27373eae48c7a4869800c68095ee0f9f0da494f83382a877"
NORTHWIND_URL = (
    f"https://github.com/jpwhite3/northwind-SQLite3/raw/{NORTHWIND_COMMIT}/dist/northwind.db"
)

NORTHWIND_DUCKDB_PATH = DATA_DIR / "northwind.duckdb"
NORTHWIND_SQLITE_PATH = DATA_DIR / "northwind.sqlite"


def download_northwind_sqlite(dest: Path = NORTHWIND_SQLITE_PATH) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        print(f"Downloading Northwind SQLite → {dest}")
        urllib.request.urlretrieve(NORTHWIND_URL, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != NORTHWIND_SHA256:
        raise ValueError(
            f"{dest} has sha256 {digest[:12]}, expected {NORTHWIND_SHA256[:12]} "
            f"(Northwind commit {NORTHWIND_COMMIT[:7]}); delete it and re-run"
        )
    return dest


def load_northwind(duckdb_path: Path = NORTHWIND_DUCKDB_PATH) -> Path:
    """Copy every base table (not the views) from the pinned SQLite file."""
    import duckdb

    sqlite_path = download_northwind_sqlite()
    if duckdb_path.exists():
        duckdb_path.unlink()
    duck = duckdb.connect(str(duckdb_path))
    try:
        path_literal = "'" + str(sqlite_path).replace("'", "''") + "'"  # ATTACH takes no parameters
        duck.execute(f"ATTACH {path_literal} AS src (TYPE sqlite, READ_ONLY)")
        tables = [
            row[0]
            for row in duck.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'src' AND table_type = 'BASE TABLE' "
                "AND table_name NOT LIKE 'sqlite_%' ORDER BY table_name"
            ).fetchall()
        ]
        for table in tables:
            ident = '"' + table.replace('"', '""') + '"'
            duck.execute(f"CREATE TABLE {ident} AS SELECT * FROM src.{ident}")
            count = duck.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0]
            print(f"  {table}: {count} rows")
        duck.execute("DETACH src")
    finally:
        duck.close()
    print(f"Wrote {duckdb_path}")
    return duckdb_path


if __name__ == "__main__":
    load_northwind()
