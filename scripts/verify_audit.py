"""Check that an audit or trace file has not been edited: python scripts/verify_audit.py data/trace.jsonl

Exits 0 when every line chains to the one before it. A file whose last lines were removed
still chains; compare the line count with what your log shipper received.
Set SECURE_QUERY_REVIEW_SECRET (and _PREVIOUS after a rotation) as the server had it.
"""

from __future__ import annotations

import sys

from secure_query.kernel.ledger import verify


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    failed = False
    for path in paths:
        intact, count, problem = verify(path)
        print(f"{path}: {'intact' if intact else 'BROKEN'}, {count} lines{'' if intact else ' - ' + problem}")
        failed |= not intact
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
