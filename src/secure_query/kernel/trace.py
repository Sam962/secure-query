"""Request-level trace: every ask / confirm / refuse / execute writes one record.

Unlike execute AuditRecord (which only fires when SQL runs), TraceRecord covers
refusals and reviews (/ask/review) so a security review can reconstruct who asked
what was refused and why.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from secure_query.kernel import ledger
from secure_query.kernel.catalog import Catalog

TraceStatus = Literal["ok", "confirm", "refuse", "error"]


@dataclass(frozen=True)
class TraceRecord:
    """One ask lifecycle event. Question text is never stored — only its hash."""

    trace_id: str
    timestamp: str
    status: TraceStatus
    principal_id: str | None
    tenant_id: str | None
    backend: str | None
    question_sha256: str
    question_length: int
    clarify_code: str | None
    model_id: str | None
    catalog_hash: str | None
    plan_hash: str | None
    sql_hash: str | None
    result_fingerprint: str | None
    duration_ms: float
    refused: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def catalog_hash(catalog: Catalog) -> str:
    """Stable short hash of the approved allowlist the principal saw."""
    payload = catalog.model_dump_json()
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def question_digest(question: str) -> tuple[str, int]:
    raw = question or ""
    return hashlib.sha256(raw.encode()).hexdigest(), len(raw)


def default_trace_path() -> Path | None:
    """SECURE_QUERY_TRACE_PATH, else same directory as the audit log, else None."""
    explicit = (os.environ.get("SECURE_QUERY_TRACE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    audit = (os.environ.get("SECURE_QUERY_AUDIT_PATH") or "data/audit.jsonl").strip()
    if not audit:
        return None
    return Path(audit).with_name("trace.jsonl")


def write_trace(record: TraceRecord, path: Path | None = None) -> None:
    target = path if path is not None else default_trace_path()
    if target is None:
        return
    ledger.append(target, record.to_json())


def make_trace(
    *,
    status: TraceStatus,
    question: str,
    principal_id: str | None = None,
    tenant_id: str | None = None,
    backend: str | None = None,
    clarify_code: str | None = None,
    model_id: str | None = None,
    catalog_hash_value: str | None = None,
    plan_hash: str | None = None,
    sql_hash: str | None = None,
    result_fingerprint: str | None = None,
    duration_ms: float = 0.0,
    refused: bool = False,
    trace_id: str | None = None,
) -> TraceRecord:
    q_hash, q_len = question_digest(question)
    return TraceRecord(
        trace_id=trace_id or uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        principal_id=principal_id,
        tenant_id=tenant_id,
        backend=backend,
        question_sha256=q_hash,
        question_length=q_len,
        clarify_code=clarify_code,
        model_id=model_id,
        catalog_hash=catalog_hash_value,
        plan_hash=plan_hash,
        sql_hash=sql_hash,
        result_fingerprint=result_fingerprint,
        duration_ms=round(duration_ms, 3),
        refused=refused,
    )


class TraceTimer:
    """Simple wall-clock helper for ask()."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000


def _canonical_rows(rows: Sequence[Sequence[Any]]) -> str:
    """Rows as text, ignoring row and column order; numbers rounded so equal answers agree."""

    def cell(v: Any) -> str:
        return f"{float(v):.4f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)

    return "\n".join(sorted("|".join(sorted(cell(c) for c in row)) for row in rows))


def answer_fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    """Order-insensitive hash of an answer: two queries agree when their fingerprints match."""
    return hashlib.sha256(_canonical_rows(rows).encode()).hexdigest()[:16]


def result_fingerprint(rows: Sequence[Sequence[Any]], columns: list[str] | None = None) -> str:
    """answer_fingerprint plus the column names, for trace and evidence records."""
    body = _canonical_rows(rows)
    if columns:
        body = ",".join(columns) + "\n" + body
    return hashlib.sha256(body.encode()).hexdigest()[:16]
