"""The product reaches a warehouse one way: the engine's connector, under the kernel.

The statements API (evals/warehouse.py) and the Genie client (evals/genie.py) load
benchmark data and query Genie with an admin token and no allowlist. They are eval
tooling, so nothing that serves a question may import them.
"""

import ast
from pathlib import Path

PRODUCT = ("api", "auth", "engine", "kernel", "planner")
ROOT = Path(__file__).resolve().parents[1] / "src" / "secure_query"


def test_product_code_never_imports_eval_tooling() -> None:
    offenders = []
    for package in PRODUCT:
        for path in (ROOT / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = (
                    [a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                )
                if any(n.startswith("secure_query.evals") for n in names):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
