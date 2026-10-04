"""Public demo datasets (Chinook, Northwind): approved catalogs, pinned loaders, CLI."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("SECURE_QUERY_DATA_DIR") or Path(__file__).resolve().parents[3] / "data"
)
"""Local, gitignored data: downloaded sources, DuckDB files, audit log."""
