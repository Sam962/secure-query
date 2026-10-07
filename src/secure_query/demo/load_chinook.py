"""Load the Chinook sample database into a local DuckDB file.

Downloads the public Chinook SQLite DB and copies every table across; falls
back to a tiny synthetic dataset with the same schema when offline.

The DDL below is the single source of truth for both paths, so an offline run
produces the same shape as a downloaded one — only the row counts differ. Note
that this mirrors the *source* database. What the planner is allowed to see is
a separate, curated decision, made in `demo/chinook.py`.

Usage:
    pip install -e ".[dev]"
    python -m secure_query.demo.load_chinook
    python -m secure_query.demo.run_query
"""

from __future__ import annotations

import hashlib
import sqlite3
import urllib.request
from pathlib import Path

from secure_query.demo import DATA_DIR

# Pinned to an upstream commit: eval reference answers depend on the exact rows
# (upstream once moved every invoice date to 2021-2025). Bump both together.
CHINOOK_COMMIT = "ac32dbc3d5b383633c3fd687934f9c719773f00d"
CHINOOK_SHA256 = "7651ba378ac2fcd0dfc3c66fb101f7a7eed3ba39a612ec642b96e20702061f15"
CHINOOK_URL = (
    f"https://github.com/lerocha/chinook-database/raw/{CHINOOK_COMMIT}/"
    "ChinookDatabase/DataSources/Chinook_Sqlite.sqlite"
)

DUCKDB_PATH = DATA_DIR / "chinook.duckdb"
SQLITE_PATH = DATA_DIR / "Chinook_Sqlite.sqlite"

SCHEMA: dict[str, str] = {
    "Artist": """
        ArtistId INTEGER, Name VARCHAR
    """,
    "Album": """
        AlbumId INTEGER, Title VARCHAR, ArtistId INTEGER
    """,
    "Genre": """
        GenreId INTEGER, Name VARCHAR
    """,
    "MediaType": """
        MediaTypeId INTEGER, Name VARCHAR
    """,
    "Playlist": """
        PlaylistId INTEGER, Name VARCHAR
    """,
    "Track": """
        TrackId INTEGER, Name VARCHAR, AlbumId INTEGER, MediaTypeId INTEGER,
        GenreId INTEGER, Composer VARCHAR, Milliseconds INTEGER, Bytes INTEGER,
        UnitPrice DOUBLE
    """,
    "PlaylistTrack": """
        PlaylistId INTEGER, TrackId INTEGER
    """,
    "Employee": """
        EmployeeId INTEGER, LastName VARCHAR, FirstName VARCHAR, Title VARCHAR,
        ReportsTo INTEGER, BirthDate TIMESTAMP, HireDate TIMESTAMP,
        Address VARCHAR, City VARCHAR, State VARCHAR, Country VARCHAR,
        PostalCode VARCHAR, Phone VARCHAR, Fax VARCHAR, Email VARCHAR
    """,
    "Customer": """
        CustomerId INTEGER, FirstName VARCHAR, LastName VARCHAR, Company VARCHAR,
        Address VARCHAR, City VARCHAR, State VARCHAR, Country VARCHAR,
        PostalCode VARCHAR, Phone VARCHAR, Fax VARCHAR, Email VARCHAR,
        SupportRepId INTEGER
    """,
    "Invoice": """
        InvoiceId INTEGER, CustomerId INTEGER, InvoiceDate TIMESTAMP,
        BillingAddress VARCHAR, BillingCity VARCHAR, BillingState VARCHAR,
        BillingCountry VARCHAR, BillingPostalCode VARCHAR, Total DOUBLE
    """,
    "InvoiceLine": """
        InvoiceLineId INTEGER, InvoiceId INTEGER, TrackId INTEGER,
        UnitPrice DOUBLE, Quantity INTEGER
    """,
}


def _columns(table: str) -> list[str]:
    return [part.split()[0] for part in SCHEMA[table].replace("\n", " ").split(",")]


def _ensure_duckdb():
    import duckdb

    return duckdb


def create_schema(duck) -> None:
    """Create every table empty. Shared by both load paths and by tests."""
    for table, columns in SCHEMA.items():
        duck.execute(f"DROP TABLE IF EXISTS {table}")
        duck.execute(f"CREATE TABLE {table} ({columns})")


def download_chinook_sqlite(dest: Path = SQLITE_PATH) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        print(f"Downloading Chinook SQLite → {dest}")
        urllib.request.urlretrieve(CHINOOK_URL, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != CHINOOK_SHA256:
        raise ValueError(
            f"{dest} has sha256 {digest[:12]}, expected {CHINOOK_SHA256[:12]} "
            f"(Chinook commit {CHINOOK_COMMIT[:7]}); delete it and re-run"
        )
    return dest


def load_from_chinook_sqlite(sqlite_path: Path, duckdb_path: Path = DUCKDB_PATH) -> Path:
    duckdb = _ensure_duckdb()
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    if duckdb_path.exists():
        duckdb_path.unlink()
    duck = duckdb.connect(str(duckdb_path))
    source = sqlite3.connect(str(sqlite_path))
    try:
        create_schema(duck)
        for table in SCHEMA:
            columns = _columns(table)
            quoted = ", ".join(f'"{c}"' for c in columns)
            rows = source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            if rows:
                placeholders = ", ".join(["?"] * len(columns))
                duck.executemany(
                    f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})", rows
                )
            print(f"  {table}: {len(rows)} rows")
    finally:
        source.close()
        duck.close()
    return duckdb_path


def load_synthetic(duckdb_path: Path = DUCKDB_PATH) -> Path:
    """Offline fallback — full schema, a handful of rows in the core tables."""
    duckdb = _ensure_duckdb()
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    if duckdb_path.exists():
        duckdb_path.unlink()
    duck = duckdb.connect(str(duckdb_path))
    try:
        create_schema(duck)
        duck.execute(
            """
            INSERT INTO Customer (CustomerId, FirstName, LastName, Address, City,
                                  Country, Phone, Email) VALUES
                (1, 'Alice', 'Nguyen', '1 Main St', 'Austin', 'USA', '555-0001', 'alice@example.com'),
                (2, 'Bruno', 'Silva', '2 Rua A', 'Rio', 'Brazil', '555-0002', 'bruno@example.com'),
                (3, 'Chloe', 'Martin', '3 Rue B', 'Paris', 'France', '555-0003', 'chloe@example.com');

            INSERT INTO Invoice (InvoiceId, CustomerId, InvoiceDate, BillingCountry, Total) VALUES
                (1, 1, '2024-01-15', 'USA', 12.50),
                (2, 1, '2024-02-10', 'USA', 8.00),
                (3, 2, '2024-01-20', 'Brazil', 20.00),
                (4, 3, '2024-03-01', 'France', 15.75),
                (5, 3, '2024-03-15', 'France', 5.25);

            INSERT INTO Genre (GenreId, Name) VALUES (1, 'Rock'), (2, 'Jazz');

            INSERT INTO Track (TrackId, Name, GenreId, UnitPrice) VALUES
                (1, 'Track One', 1, 0.99), (2, 'Track Two', 2, 0.99);

            INSERT INTO InvoiceLine (InvoiceLineId, InvoiceId, TrackId, UnitPrice, Quantity) VALUES
                (1, 1, 1, 0.99, 1), (2, 1, 2, 0.99, 1), (3, 3, 1, 0.99, 1);

            INSERT INTO Employee (EmployeeId, LastName, FirstName, Title, Email) VALUES
                (1, 'Adams', 'Andrew', 'General Manager', 'andrew@chinook.test'),
                (2, 'Edwards', 'Nancy', 'Sales Manager', 'nancy@chinook.test');
            """
        )
        print("Loaded synthetic Chinook-shaped sample (offline fallback)")
    finally:
        duck.close()
    return duckdb_path


def load_sample_db(*, prefer_download: bool = True, strict: bool = False) -> Path:
    """Return path to data/chinook.duckdb, downloading Chinook when possible.

    `strict` disables the synthetic fallback: evals scored against synthetic
    rows are meaningless, so CI must fail rather than fall back.
    """
    if prefer_download:
        try:
            sqlite_path = download_chinook_sqlite()
            path = load_from_chinook_sqlite(sqlite_path)
            print(f"Wrote {path}")
            return path
        except Exception as exc:  # network / parse / checksum issues
            if strict:
                raise
            print(f"Download failed ({exc}); using synthetic data")
    path = load_synthetic()
    print(f"Wrote {path}")
    return path


if __name__ == "__main__":
    import sys

    load_sample_db(prefer_download=True, strict="--strict" in sys.argv[1:])
