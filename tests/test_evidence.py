"""Signed Evidence Bundle round-trip and ask() wiring."""

from __future__ import annotations

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.kernel.evidence import (
    METRIC_CHECKS,
    PLAN_PATH_CHECKS,
    SQL_PATH_CHECKS,
    EvidenceBundle,
    build_evidence,
)
from secure_query.planner import MockLLMClient
from secure_query.planner.confidence import link_tables


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
        policy_checks=SQL_PATH_CHECKS,
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
    assert tuple(outcome.evidence["policy_checks"]) == PLAN_PATH_CHECKS


def test_evidence_lists_the_checks_of_the_path_that_ran(monkeypatch, tmp_path) -> None:
    import json

    import pytest

    monkeypatch.setenv("SECURE_QUERY_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    who = Principal(principal_id="demo-user", tenant_id="chinook")

    monkeypatch.setenv("SECURE_QUERY_PLANNER", "sql")
    sql_path = ask(
        "How many invoices are there?",
        principal=who,
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Invoice"})]),
        confirm_only=True,
    )
    assert sql_path.status == "confirm"
    assert tuple(sql_path.evidence["policy_checks"]) == SQL_PATH_CHECKS

    monkeypatch.setenv("SECURE_QUERY_PLANNER", "lqp")
    metric = ask(
        "line item revenue",
        principal=who,
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(responses=[json.dumps({"metric_id": "line_item_revenue"})]),
        confirm_only=True,
    )
    assert metric.status == "confirm"
    assert tuple(metric.evidence["policy_checks"]) == METRIC_CHECKS

    with pytest.raises(ValueError, match="policy_checks"):
        build_evidence(kind="ok", trace_id="t", question="q")


def test_link_tables_returns_confidence() -> None:
    decision = link_tables("revenue by country", sample_catalog(), k=4)
    assert decision.choice
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.alternatives
