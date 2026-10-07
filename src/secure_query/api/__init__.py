"""HTTP API package.

`import secure_query` loads `api.service` (the ask pipeline) and must not pull
in FastAPI. Import the app from `secure_query.api.http`, or via `from
secure_query.api import app` which loads it lazily.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from secure_query.api.http import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
