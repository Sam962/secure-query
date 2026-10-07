"""Core import graph: `import secure_query` must not load FastAPI (M12)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_import_secure_query_does_not_load_fastapi() -> None:
    env = {**os.environ, "PYTHONPATH": _SRC}
    script = (
        "import secure_query, sys; "
        "assert 'fastapi' not in sys.modules, sorted(k for k in sys.modules if 'fast' in k)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=False, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_package_version_matches_pyproject() -> None:
    import tomllib
    from importlib.metadata import version

    expected = tomllib.loads(Path("pyproject.toml").read_text()).get("project", {}).get("version")
    assert version("secure-query") == expected
