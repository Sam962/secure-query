"""Catalog draft from DuckDB information_schema is unapproved."""

from __future__ import annotations

from pathlib import Path

import duckdb

from secure_query.engine.catalog_draft import draft_from_duckdb
from secure_query.examples.load_sample_db import create_schema


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


def test_display_column_shorthand_and_bad_label_refs() -> None:
    import pytest
    from pydantic import ValidationError

    from secure_query.kernel.catalog import Catalog

    def catalog(display: str, label_for: str | None = None) -> Catalog:
        return Catalog.model_validate(
            {
                "tenant_id": "t",
                "tables": [
                    {"name": "dept", "display_column": display,
                     "columns": [{"name": "id", "dtype": "int"}, {"name": "name", "dtype": "str"}]},
                    {"name": "staff", "columns": [{"name": "dept_id", "dtype": "int", "label_for": label_for}]},
                ],
            }
        )

    assert catalog("name").table_map()["dept"].display_column == "dept.name"
    assert catalog("dept.name", "dept.name").table_map()["staff"].columns[0].label_for == "dept.name"
    with pytest.raises(ValidationError, match="display_column"):
        catalog("title")
    with pytest.raises(ValidationError, match="label_for"):
        catalog("name", "dept.title")
