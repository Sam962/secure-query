"""Every ask / confirm / refuse writes a TraceRecord (not only executes)."""

from __future__ import annotations

import json
from pathlib import Path

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.kernel.trace import catalog_hash, make_trace, write_trace
from secure_query.planner import MockLLMClient


def test_refuse_writes_trace(tmp_path: Path, monkeypatch) -> None:
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SECURE_QUERY_TRACE_PATH", str(trace_path))
    outcome = ask(
        "SELECT email FROM Customer",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        review_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "sql_in_question"
    assert outcome.trace_id
    lines = trace_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "refuse"
    assert record["clarify_code"] == "sql_in_question"
    assert record["trace_id"] == outcome.trace_id
    assert record["question_sha256"]
    assert "question" not in record


def test_confirm_and_execute_write_traces(tmp_path: Path, monkeypatch) -> None:
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SECURE_QUERY_TRACE_PATH", str(trace_path))
    principal = Principal(principal_id="demo-user", tenant_id="chinook")
    config = runtime_config()
    catalog = sample_catalog()
    client = MockLLMClient()
    confirm = ask(
        "revenue by country",
        principal=principal,
        config=config,
        catalog=catalog,
        client=client,
        review_only=True,
    )
    assert confirm.status == "confirm"
    assert confirm.trace_id
    executed = ask(
        "revenue by country",
        principal=principal,
        config=config,
        catalog=catalog,
        client=MockLLMClient(),
        execute=True,
    )
    assert executed.status == "ok"
    assert executed.audit.get("result_fingerprint")
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert {r["status"] for r in records} >= {"confirm", "ok"}
    assert all(r.get("catalog_hash") for r in records if r["status"] in {"confirm", "ok"})


def test_catalog_hash_is_stable() -> None:
    a = catalog_hash(sample_catalog())
    b = catalog_hash(sample_catalog())
    assert a == b
    assert len(a) == 16


def test_write_trace_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    record = make_trace(status="refuse", question="hi", clarify_code="auth", refused=True)
    write_trace(record, path)
    loaded = json.loads(path.read_text().strip())
    assert loaded["trace_id"] == record.trace_id
    assert loaded["status"] == "refuse"


def test_trace_file_shows_edits_and_removed_lines(tmp_path: Path, monkeypatch) -> None:
    from secure_query.kernel import ledger

    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "a-secret-a-secret-a-secret-a-secret")
    path = tmp_path / "t.jsonl"
    for code in ("auth", "planner_refusal", "unstable_answer"):
        write_trace(make_trace(status="refuse", question="hi", clarify_code=code, refused=True), path)
    assert ledger.verify(path) == (True, 3, "")

    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # a refusal quietly removed
    assert ledger.verify(path)[:2] == (False, 2)
    path.write_text("\n".join([lines[0], lines[1].replace("planner_refusal", "ok"), lines[2]]) + "\n")
    assert ledger.verify(path)[:2] == (False, 2)

    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "another-secret-another-secret-12345")
    path.write_text("\n".join(lines) + "\n")
    assert not ledger.verify(path)[0]  # the chain cannot be recomputed without the secret
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET_PREVIOUS", "a-secret-a-secret-a-secret-a-secret")
    assert ledger.verify(path)[0]


def test_trace_lines_from_many_threads_stay_chained(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from secure_query.kernel import ledger

    path = tmp_path / "t.jsonl"
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: write_trace(make_trace(status="refuse", question=str(i), refused=True), path), range(40)))
    assert ledger.verify(path) == (True, 40, "")
