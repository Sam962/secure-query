"""Signed Evidence Bundle round-trip and ask() wiring."""

from __future__ import annotations

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.kernel.evidence import EvidenceBundle, build_evidence
from secure_query.kernel.sql_validate import validate_sql
from secure_query.planner import MockLLMClient
from secure_query.planner.confidence import link_tables
from secure_query.planner.dpc_lite import dpc_lite_check, shadow_count_sql


def test_evidence_signs_and_verifies() -> None:
    raw = build_evidence(
        kind="ok",
        trace_id="abc",
        question="revenue by country",
        principal_id="demo-user",
        tenant_id="chinook",
        catalog_hash="deadbeef",
        plan_hash="sql:1234",
        sql_hash="abcd",
        result_fingerprint="fp1",
    )
    assert raw["signature"]
    bundle = EvidenceBundle(**{**raw, "policy_checks": tuple(raw["policy_checks"]), "gate_fingerprints": tuple(raw.get("gate_fingerprints") or ())})
    assert bundle.verify()
    tampered = EvidenceBundle(**{**raw, "policy_checks": ("tampered",), "gate_fingerprints": (), "signature": raw["signature"]})
    assert not tampered.verify()


def test_ask_refuse_includes_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SECURE_QUERY_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    outcome = ask(
        "SELECT email FROM Customer",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
    )
    assert outcome.status == "clarify"
    assert outcome.evidence is not None
    assert outcome.evidence["kind"] == "refuse"
    assert outcome.evidence["signature"]


def test_ask_confirm_includes_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SECURE_QUERY_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("SECURE_QUERY_PLANNER", "lqp")
    outcome = ask(
        "revenue by country",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.status == "confirm"
    assert outcome.evidence is not None
    assert outcome.evidence["kind"] == "confirm"
    assert "allowlist_columns" in outcome.evidence["policy_checks"]


def test_link_tables_returns_confidence() -> None:
    decision = link_tables("revenue by country", sample_catalog(), k=4)
    assert decision.choice
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.alternatives


def test_dpc_lite_shadow_count(monkeypatch) -> None:
    from pathlib import Path

    import duckdb

    from secure_query.demo.load_chinook import DUCKDB_PATH

    if not Path(DUCKDB_PATH).exists():
        return
    catalog = sample_catalog()
    compiled = validate_sql("SELECT COUNT(*) AS n FROM Invoice", catalog).compiled
    shadow = shadow_count_sql(compiled, catalog)
    assert shadow is not None
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    def probe(c):
        return con.execute(c.sql).fetchall()

    ok, fps = dpc_lite_check(compiled, catalog, probe)
    assert ok
    assert fps  # unique fingerprints; COUNT(*) vs COUNT(1) agree → one fp
