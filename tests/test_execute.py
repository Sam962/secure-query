"""Phase 3 execute tests — timeout, row cap, audit stub."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import duckdb
import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.demo.load_chinook import create_schema
from secure_query.engine.execute import (
    ExecuteOptions,
    ExecutionError,
    execute_duckdb,
)
from secure_query.kernel.builder import LQP
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.validate import validate_and_compile


@pytest.fixture()
def tiny_db(tmp_path: Path) -> Path:
    """Tiny DB built from the shared schema, so it cannot drift from the catalog."""
    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    create_schema(con)
    con.execute(
        """
        INSERT INTO Customer (CustomerId, FirstName, LastName, Email, Country) VALUES
            (1, 'A', 'One', 'a@x.com', 'USA'),
            (2, 'B', 'Two', 'b@x.com', 'Canada');
        INSERT INTO Invoice (InvoiceId, CustomerId, InvoiceDate, BillingCountry, Total) VALUES
            (1, 1, '2024-01-01', 'USA', 10.0),
            (2, 1, '2024-01-02', 'USA', 5.0),
            (3, 2, '2024-01-03', 'Canada', 7.0);
        """
    )
    con.close()
    return path


def test_execute_ok_and_audit_file(tiny_db: Path, tmp_path: Path) -> None:
    catalog = sample_catalog()
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
        .group_by_columns(["Customer.Country"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .order_by("revenue", direction="desc")
        .limit(10)
        .build()
    )
    compiled = validate_and_compile(plan, catalog)
    audit_path = tmp_path / "audit.jsonl"
    result = execute_duckdb(
        compiled,
        tiny_db,
        plan=plan,
        question="revenue by country",
        options=ExecuteOptions(timeout_seconds=10, max_rows=100, audit_path=audit_path),
    )
    assert result.columns == ["Country", "revenue"]
    assert result.rows[0][0] == "USA"
    assert result.truncated is False
    assert result.audit.status == "ok"
    assert result.audit.plan_hash == compiled.plan_hash
    assert audit_path.exists()
    line = json.loads(audit_path.read_text().strip().splitlines()[-1])
    assert line["status"] == "ok"
    # M1: the audit log never stores the question text.
    assert "question" not in line and "revenue by country" not in json.dumps(line)
    assert line["question_sha256"] == hashlib.sha256(b"revenue by country").hexdigest()
    assert line["question_length"] == len("revenue by country")
    assert line["audit_id"]
    assert "SELECT" in line["sql"]
    assert "principal_id" in line
    assert "tenant_id" in line


def test_row_cap_truncates(tiny_db: Path) -> None:
    catalog = sample_catalog()
    plan = (
        LQP.filter_and_list(table="Customer")
        .order_by("Customer.CustomerId", direction="asc")
        .limit(100)
        .build()
    )
    compiled = validate_and_compile(plan, catalog)
    result = execute_duckdb(
        compiled,
        tiny_db,
        plan=plan,
        options=ExecuteOptions(max_rows=1),
    )
    assert len(result.rows) == 1
    assert result.truncated is True
    assert result.audit.truncated is True
    assert result.audit.row_count == 1


def test_timeout_raises_without_waiting_for_the_query(
    tiny_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller must be released at the timeout, not when the query finishes."""
    import secure_query.engine.execute as ex

    def slow(*_a, **_k):
        time.sleep(3)
        return ["x"], [], False

    monkeypatch.setattr(ex, "_execute_once", slow)
    compiled = CompiledQuery(
        sql="SELECT 1",
        plan_hash="p",
        sql_hash="s",
        parameters=[],
    )
    started = time.perf_counter()
    with pytest.raises(ExecutionError, match="timeout"):
        execute_duckdb(
            compiled,
            tiny_db,
            options=ExecuteOptions(timeout_seconds=0.2, max_rows=10),
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"timeout did not bound wall clock: took {elapsed:.2f}s"


def test_timeout_interrupts_a_real_query(tiny_db: Path) -> None:
    compiled = CompiledQuery(
        sql="SELECT COUNT(*) FROM range(200000000) a, range(200000) b",
        plan_hash="p",
        sql_hash="s",
        parameters=[],
    )
    started = time.perf_counter()
    with pytest.raises(ExecutionError, match="timeout"):
        execute_duckdb(
            compiled,
            tiny_db,
            options=ExecuteOptions(timeout_seconds=0.5, max_rows=10),
        )
    assert time.perf_counter() - started < 3.0


def test_timeout_is_audited(tiny_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import secure_query.engine.execute as ex

    monkeypatch.setattr(ex, "_execute_once", lambda *a, **k: (time.sleep(2), None)[1])
    audit_path = tmp_path / "audit.jsonl"
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    with pytest.raises(ExecutionError):
        execute_duckdb(
            compiled,
            tiny_db,
            options=ExecuteOptions(timeout_seconds=0.1, audit_path=audit_path),
        )
    record = json.loads(audit_path.read_text().strip())
    assert record["status"] == "timeout"
    assert record["row_count"] == 0


def test_missing_db_raises(tmp_path: Path) -> None:
    compiled = CompiledQuery(sql="SELECT 1", plan_hash="p", sql_hash="s", parameters=[])
    with pytest.raises(ExecutionError, match="not found"):
        execute_duckdb(compiled, tmp_path / "nope.duckdb")


def test_timeout_leaves_no_worker_running(tiny_db: Path) -> None:
    """M3: after a timeout the interrupted query's worker thread is joined, not leaked."""
    import threading

    compiled = CompiledQuery(
        sql="SELECT COUNT(*) FROM range(200000000) a, range(200000) b",
        plan_hash="p",
        sql_hash="s",
        parameters=[],
    )
    before = set(threading.enumerate())
    with pytest.raises(ExecutionError, match="timeout") as info:
        execute_duckdb(compiled, tiny_db, options=ExecuteOptions(timeout_seconds=0.3))
    assert info.value.audit_id  # M2: the client gets a reference, the log has the detail
    left = [t for t in set(threading.enumerate()) - before if t.name.startswith("sq-duckdb")]
    assert not left
