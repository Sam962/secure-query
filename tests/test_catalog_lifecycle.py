"""Catalog approval record and drift check."""

from __future__ import annotations

import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.engine.catalog_draft import catalog_drift, main
from secure_query.kernel.catalog import CatalogApproval, production_problems
from secure_query.kernel.trace import catalog_hash
from tests.conftest import requires_chinook

APPROVAL = CatalogApproval(version="2026.11.1", approved_by="data-owner@example.com", approved_at="2026-11-13")


def test_production_refuses_an_unapproved_catalog() -> None:
    base = sample_catalog()
    assert any("approval" in p for p in production_problems(base))
    approved = base.model_copy(update={"approval": APPROVAL, "allow_any_join": False})
    assert production_problems(approved) == []
    assert any("allow_any_join" in p for p in production_problems(approved.model_copy(update={"allow_any_join": True})))
    assert catalog_hash(approved) != catalog_hash(base)  # evidence names the approved version


def test_api_does_not_start_in_production_without_an_approved_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from secure_query.api.http import app

    monkeypatch.setenv("SECURE_QUERY_ENV", "production")
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "s" * 40)
    with pytest.raises(RuntimeError, match="approval"):
        with TestClient(app):
            pass


def test_drift_reports_missing_and_changed_as_breaking_and_extra_as_new() -> None:
    approved = sample_catalog()
    tables = []
    for table in approved.tables:
        if table.name == "Genre":
            continue  # dropped in the warehouse
        columns = list(table.columns)
        if table.name == "Track":
            columns = [c for c in columns if c.name != "Composer"]  # column dropped
            columns = [c.model_copy(update={"dtype": "string"}) if c.name == "Milliseconds" else c for c in columns]
        if table.name == "Artist":
            columns.append(columns[0].model_copy(update={"name": "Country"}))  # column added
        tables.append(table.model_copy(update={"columns": columns}))
    breaking, new = catalog_drift(approved, approved.model_copy(update={"tables": tables}))
    assert len(breaking) == 3
    assert any("table Genre" in b for b in breaking) and any("Track.Composer" in b for b in breaking)
    assert any("Track.Milliseconds is string" in b for b in breaking)
    assert new == ["column Artist.Country is not in the catalog"]
    assert catalog_drift(approved, approved) == ([], [])


@requires_chinook
def test_drift_check_command_passes_on_a_fresh_draft(tmp_path, capsys) -> None:
    from secure_query.demo.load_chinook import DUCKDB_PATH

    draft = tmp_path / "catalog.json"
    assert main(["--duckdb", str(DUCKDB_PATH), "-o", str(draft)]) == 0
    assert main(["--duckdb", str(DUCKDB_PATH), "--check", str(draft)]) == 0
    assert "0 breaking" in capsys.readouterr().out
