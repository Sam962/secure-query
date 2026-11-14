"""Signing keys for reviews and Evidence Bundles.

`SECURE_QUERY_REVIEW_SECRET` signs. `SECURE_QUERY_REVIEW_SECRET_PREVIOUS` (comma-separated)
is accepted for verification only, so a secret can be rotated without failing what was
signed just before. Without a secret each process uses a random key (one worker, demo only).
"""

from __future__ import annotations

import base64
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


# --- Evidence: asymmetric signing, so an auditor can verify without the secret ---------

_ED25519 = "ed25519"


def _pem(value: str) -> bytes:
    """An env value that is either PEM text or the path of a PEM file."""
    value = value.strip()
    if value.startswith("-----"):
        return value.replace("\\n", "\n").encode()
    with open(value, "rb") as fh:
        return fh.read()


@lru_cache(maxsize=4)
def _private_key(raw: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(_pem(raw), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("SECURE_QUERY_EVIDENCE_PRIVATE_KEY must be an Ed25519 private key (PEM)")
    return key


def evidence_public_key() -> dict[str, str] | None:
    """The key an auditor needs, or None when evidence is HMAC-signed."""
    raw = os.environ.get("SECURE_QUERY_EVIDENCE_PRIVATE_KEY") or ""
    if not raw.strip():
        return None
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    public = _private_key(raw).public_key()
    key_id = hashlib.sha256(public.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()[:16]
    pem = public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return {"algorithm": _ED25519, "key_id": key_id, "public_key_pem": pem}


def sign_evidence(message: str) -> str:
    """`ed25519:<key_id>:<base64 signature>` when a private key is configured, else an HMAC."""
    public = evidence_public_key()
    if public is None:
        return mac(message)
    signature = _private_key(os.environ["SECURE_QUERY_EVIDENCE_PRIVATE_KEY"]).sign(message.encode())
    return f"{_ED25519}:{public['key_id']}:{base64.b64encode(signature).decode()}"


def evidence_matches(message: str, signature: str, public_key_pem: str | None = None) -> bool:
    """Verify an evidence signature. `public_key_pem` lets a third party verify without this server."""
    if not signature.startswith(f"{_ED25519}:"):
        return public_key_pem is None and mac_matches(message, signature)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    if public_key_pem is None:
        public = evidence_public_key()
        if public is None:
            return False
        public_key_pem = public["public_key_pem"]
    try:
        raw = base64.b64decode(signature.rsplit(":", 1)[1], validate=True)
        load_pem_public_key(public_key_pem.encode()).verify(raw, message.encode())
    except (InvalidSignature, ValueError):
        return False
    return True
