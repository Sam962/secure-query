"""Active demo catalog — Chinook."""

from secure_query.examples.sample_catalog import demo_catalog, sample_catalog

__all__ = ["demo_catalog", "sample_catalog"]


if __name__ == "__main__":
    from secure_query.builder import LQP
    from secure_query.validate import validate_and_compile

    catalog = demo_catalog()
    print("--- catalog sent to LLM planner ---")
    print(catalog.planner_summary())
    print()

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
    print("--- compiled SQL ---")
    print(compiled.sql)
    print("plan_hash:", compiled.plan_hash[:16], "...")
