"""Process-entrypoint .env loader. Do not import from library internals.

Keys already in the environment win. Values are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> None:
    """Apply KEY=VALUE lines from `.env` via setdefault. Missing file is a no-op."""
    target = Path(path) if path is not None else Path(".env")
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
