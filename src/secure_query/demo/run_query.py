"""End-to-end local demo: validate → AST compile → execute (Phase 3 hardened).

Requires: pip install -e ".[dev]"
         python -m secure_query.demo.load_chinook
"""

from __future__ import annotations

from pathlib import Path

from secure_query.demo.chinook import sample_catalog
from secure_query.demo.load_chinook import DATA_DIR, DUCKDB_PATH, load_sample_db
from secure_query.demo.lqp import LQP
from secure_query.engine.execute import ExecuteOptions, execute_duckdb
from secure_query.kernel.validate import validate_and_compile


def main() -> None:
    if not DUCKDB_PATH.exists():
        load_sample_db(prefer_download=True)

    catalog = sample_catalog()
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
        .group_by_columns(["Customer.Country"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .order_by("revenue", direction="desc")
        .limit(10)
        .build()
    )
    compiled = validate_and_compile(plan, catalog)
    audit_path = DATA_DIR / "audit.jsonl"
    result = execute_duckdb(
        compiled,
        DUCKDB_PATH,
        plan=plan,
        question="(demo) revenue by country",
        options=ExecuteOptions(
            timeout_seconds=30,
            max_rows=catalog.max_limit,
            audit_path=audit_path,
        ),
    )

    print("--- planner summary ---")
    print(catalog.planner_summary())
    print()
    print("--- SQL ---")
    print(compiled.sql)
    print()
    print("--- results ---")
    print(result.columns)
    for row in result.rows:
        print(row)
    print()
    print("plan_hash:", result.audit.plan_hash[:16], "...")
    print("duration_ms:", round(result.duration_ms, 1))
    print("audit:", Path(audit_path))
    print("db:", Path(DUCKDB_PATH))


if __name__ == "__main__":
    main()
