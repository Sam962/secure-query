"""Signed Evidence Bundle — why an answer (or refusal) was allowed.

Built on the same HMAC family as review signatures. Clients can re-verify the
bundle without trusting the UI. Question text is never included — only its hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

EvidenceKind = Literal["ok", "confirm", "refuse"]


@lru_cache(maxsize=1)
def _evidence_secret() -> bytes:
    return (os.environ.get("SECURE_QUERY_REVIEW_SECRET") or "").encode() or secrets.token_bytes(32)


@dataclass(frozen=True)
class EvidenceBundle:
    """Verifiable record of what the kernel decided."""

    version: str
    kind: EvidenceKind
    trace_id: str
    timestamp: str
    principal_id: str | None
    tenant_id: str | None
    question_sha256: str
    catalog_hash: str | None
    model_id: str | None
    plan_hash: str | None
    sql_hash: str | None
    result_fingerprint: str | None
    clarify_code: str | None
    policy_checks: tuple[str, ...]
    gate_fingerprints: tuple[str, ...] = ()
    signature: str = ""

    def payload_for_sign(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("signature", None)
        return data

    def sign(self) -> EvidenceBundle:
        body = json.dumps(self.payload_for_sign(), sort_keys=True, separators=(",", ":"), default=str)
        sig = hmac.new(_evidence_secret(), body.encode(), hashlib.sha256).hexdigest()
        return EvidenceBundle(**{**asdict(self), "signature": sig})

    def verify(self) -> bool:
        if not self.signature:
            return False
        body = json.dumps(self.payload_for_sign(), sort_keys=True, separators=(",", ":"), default=str)
        expected = hmac.new(_evidence_secret(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The checks each path runs. A bundle lists only the checks of the path that produced the query.
SQL_PATH_CHECKS = (
    "allowlist_columns",
    "no_pii_high",
    "approved_joins",
    "fan_out",
    "row_filters",
    "read_only",
    "limit_capped",
)
PLAN_PATH_CHECKS = (
    "catalog_columns",
    "no_pii_high",
    "approved_joins",
    "row_filters",
    "read_only",
    "limit_capped",
)
METRIC_CHECKS = ("approved_metric", "read_only")


def build_evidence(
    *,
    kind: EvidenceKind,
    trace_id: str,
    question: str,
    principal_id: str | None = None,
    tenant_id: str | None = None,
    catalog_hash: str | None = None,
    model_id: str | None = None,
    plan_hash: str | None = None,
    sql_hash: str | None = None,
    result_fingerprint: str | None = None,
    clarify_code: str | None = None,
    policy_checks: tuple[str, ...] | None = None,
    gate_fingerprints: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a signed evidence dict ready for AskOutcome / API responses.

    `policy_checks` is required for kinds "ok" and "confirm": the caller names the
    checks of the path that produced the query.
    """
    if policy_checks is None:
        if kind != "refuse":
            raise ValueError(f"build_evidence(kind={kind!r}) requires policy_checks")
        policy_checks = ("refused",)
    checks = policy_checks
    q_hash = hashlib.sha256((question or "").encode()).hexdigest()
    bundle = EvidenceBundle(
        version="evidence/1",
        kind=kind,
        trace_id=trace_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        principal_id=principal_id,
        tenant_id=tenant_id,
        question_sha256=q_hash,
        catalog_hash=catalog_hash,
        model_id=model_id,
        plan_hash=plan_hash,
        sql_hash=sql_hash,
        result_fingerprint=result_fingerprint,
        clarify_code=clarify_code,
        policy_checks=checks,
        gate_fingerprints=gate_fingerprints,
    ).sign()
    return bundle.to_dict()
