"""Signing keys for reviews and Evidence Bundles.

`SECURE_QUERY_REVIEW_SECRET` signs. `SECURE_QUERY_REVIEW_SECRET_PREVIOUS` (comma-separated)
is accepted for verification only, so a secret can be rotated without failing what was
signed just before. Without a secret each process uses a random key (one worker, demo only).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import lru_cache


@lru_cache(maxsize=1)
def _process_key() -> bytes:
    return secrets.token_bytes(32)


def signing_key() -> bytes:
    return (os.environ.get("SECURE_QUERY_REVIEW_SECRET") or "").strip().encode() or _process_key()


def verification_keys() -> list[bytes]:
    previous = (os.environ.get("SECURE_QUERY_REVIEW_SECRET_PREVIOUS") or "").split(",")
    return [signing_key(), *(p.strip().encode() for p in previous if p.strip())]


def mac(message: str, key: bytes | None = None) -> str:
    return hmac.new(key or signing_key(), message.encode(), hashlib.sha256).hexdigest()


def mac_matches(message: str, signature: str) -> bool:
    """True when any accepted key produced `signature`. Every key is tried (constant work)."""
    results = [hmac.compare_digest(mac(message, key), signature or "") for key in verification_keys()]
    return any(results)
