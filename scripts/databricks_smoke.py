#!/usr/bin/env python3
"""Live Databricks smoke: validated SQL → execute as the end user → signed evidence.

Checks the warehouse leg only (no LLM): allowlist validation, end-user token,
read-only execution, and an Evidence Bundle that verifies.

Requires a real workspace. Skips cleanly when credentials are absent.

  SECURE_QUERY_DATABRICKS_AUTH=user
  DATABRICKS_HOST / DATABRICKS_HTTP_PATH / SECURE_QUERY_DATABRICKS_SCHEMA
  SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN  (end-user PAT or OBO token)
  SECURE_QUERY_CATALOG_FILE           (approved catalog for the schema)
  SECURE_QUERY_SMOKE_QUESTION         (default: a simple COUNT)

Exit 0 on success; exit 2 when skipped; exit 1 on failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlglot import exp  # noqa: E402

from secure_query.auth import Principal  # noqa: E402
from secure_query.engine.env import load_dotenv  # noqa: E402
from secure_query.engine.runtime import RuntimeConfig, execute_compiled_query  # noqa: E402
from secure_query.kernel.catalog import Catalog  # noqa: E402
from secure_query.kernel.evidence import SQL_PATH_CHECKS, EvidenceBundle, build_evidence  # noqa: E402
from secure_query.kernel.sql_validate import validate_sql  # noqa: E402
from secure_query.kernel.trace import catalog_hash, result_fingerprint  # noqa: E402


def main() -> int:
    load_dotenv()
    host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    token = os.environ.get("SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN")
    catalog_file = os.environ.get("SECURE_QUERY_CATALOG_FILE")
    schema = os.environ.get("SECURE_QUERY_DATABRICKS_SCHEMA")
    if not (host and token and catalog_file and schema and Path(catalog_file).exists()):
        print(
            "skip: set DATABRICKS_HOST, SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN, "
            "SECURE_QUERY_DATABRICKS_SCHEMA, SECURE_QUERY_CATALOG_FILE",
            file=sys.stderr,
        )
        return 2

    os.environ.setdefault("SECURE_QUERY_DATABRICKS_AUTH", "user")
    os.environ.setdefault("SECURE_QUERY_BACKEND", "databricks")
    catalog = Catalog.model_validate_json(Path(catalog_file).read_text(encoding="utf-8"))
    if not catalog.tables:
        print("fail: catalog has no tables", file=sys.stderr)
        return 1
    t0 = catalog.tables[0].name
    default_sql = exp.select(exp.alias_(exp.Count(this=exp.Star()), "n")).from_(exp.to_table(t0))
    sql = os.environ.get("SECURE_QUERY_SMOKE_SQL") or default_sql.sql(dialect=catalog.sql_dialect)
    question = os.environ.get("SECURE_QUERY_SMOKE_QUESTION") or sql
    validated = validate_sql(sql, catalog)

    principal = Principal(
        principal_id=os.environ.get("SECURE_QUERY_PRINCIPAL_ID") or "smoke-user",
        tenant_id=catalog.tenant_id,
        warehouse_token=token,
    )
    config = RuntimeConfig(
        backend="databricks",
        catalog=catalog,
        audit_path=Path(os.environ.get("SECURE_QUERY_AUDIT_PATH") or "data/audit.jsonl"),
        duckdb_path=Path("data/chinook.duckdb"),
    )
    result = execute_compiled_query(
        validated.compiled,
        config,
        question=question,
        principal=principal,
    )
    evidence = build_evidence(
        kind="ok",
        trace_id="smoke",
        question=question,
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        catalog_hash=catalog_hash(catalog),
        plan_hash=validated.compiled.plan_hash,
        sql_hash=validated.compiled.sql_hash,
        result_fingerprint=result_fingerprint(result.rows, list(result.columns)),
        policy_checks=SQL_PATH_CHECKS,
    )
    bundle = EvidenceBundle(
        **{
            **evidence,
            "policy_checks": tuple(evidence["policy_checks"]),
            "gate_fingerprints": tuple(evidence.get("gate_fingerprints") or ()),
        }
    )
    assert bundle.verify(), "evidence signature failed"
    print(f"ok rows={len(result.rows)} cols={result.columns} evidence={evidence['signature'][:16]}…")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"fail: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
