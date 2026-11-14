"""Append-only JSONL with a hash chain, so removed or edited lines are detectable.

Each line gains a `chain` field computed from the previous line's chain and this line's
content. With SECURE_QUERY_REVIEW_SECRET the chain is an HMAC (`m1:`), which someone who can
edit the file cannot recompute; without it, a plain hash (`h1:`). Writes are serialised with
a process lock and a file lock, so several workers can share one file.

SECURE_QUERY_AUDIT_STDOUT=1 also prints each line to stdout for a log shipper.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

from secure_query.kernel.signing import mac, mac_matches

try:  # POSIX only; on other systems the process lock still applies
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_LOCK = threading.Lock()
_GENESIS = "genesis"


def _has_secret() -> bool:
    return bool((os.environ.get("SECURE_QUERY_REVIEW_SECRET") or "").strip())


def _link(previous: str, body: str) -> str:
    message = f"{previous}\n{body}"
    return f"m1:{mac(message)}" if _has_secret() else f"h1:{hashlib.sha256(message.encode()).hexdigest()}"


def _link_matches(previous: str, body: str, chain: str) -> bool:
    message = f"{previous}\n{body}"
    if chain.startswith("m1:"):
        return mac_matches(message, chain[3:])
    return chain == f"h1:{hashlib.sha256(message.encode()).hexdigest()}"


def _last_chain(fh) -> str:
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if size == 0:
        return _GENESIS
    fh.seek(max(0, size - 65536))
    lines = fh.read().decode("utf-8", "replace").splitlines()
    try:
        return str(json.loads(lines[-1])["chain"])
    except (IndexError, KeyError, ValueError, TypeError):
        return "unchained"  # a file written before chaining, or a damaged last line


def append(path: Path | str, record_json: str) -> None:
    """Append one JSON object as a chained line."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(json.loads(record_json), sort_keys=True, separators=(",", ":"))
    with _LOCK, target.open("a+b") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        chain = _link(_last_chain(fh), body)
        line = body[:-1] + f',"chain":"{chain}"}}'
        fh.seek(0, os.SEEK_END)
        fh.write(line.encode() + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    if (os.environ.get("SECURE_QUERY_AUDIT_STDOUT") or "").strip().lower() in ("1", "true", "yes"):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def verify(path: Path | str) -> tuple[bool, int, str]:
    """(intact, lines checked, problem). Lines written before chaining count as a break."""
    previous = _GENESIS
    count = 0
    with Path(path).open(encoding="utf-8") as fh:
        for count, raw in enumerate(fh, start=1):
            try:
                record = json.loads(raw)
                chain = str(record.pop("chain"))
            except (ValueError, KeyError, AttributeError):
                return False, count, f"line {count}: not a chained record"
            body = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if not _link_matches(previous, body, chain):
                return False, count, f"line {count}: changed, or a line before it was removed or edited"
            previous = chain
    return True, count, ""
