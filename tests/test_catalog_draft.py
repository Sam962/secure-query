"""Catalog draft from DuckDB information_schema is unapproved."""

from __future__ import annotations

from pathlib import Path

import duckdb

from secure_query.demo.load_chinook import create_schema
from secure_query.engine.catalog_draft import draft_from_duckdb


def test_draft_from_tiny_duckdb(tmp_path: Path) -> None:
    path = tmp_path / "draft.duckdb"
    con = duckdb.connect(str(path))
    create_schema(con)
    con.close()
    payload = draft_from_duckdb(path, tenant_id="draft")
    names = {t["name"] for t in payload["tables"]}
    assert "Invoice" in names
    assert "Customer" in names
    assert payload["tenant_id"] == "draft"
    assert payload["join_keys"] == []
