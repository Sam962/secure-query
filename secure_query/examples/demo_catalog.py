"""Demo approved catalog — replace with your domain tables."""

from secure_query.catalog import Catalog, ColumnSpec, JoinKey, TableSpec


def demo_catalog() -> Catalog:
    return Catalog(
        tenant_id="demo",
        require_limit=True,
        max_limit=1000,
        tables=[
            TableSpec(
                name="orders",
                description="Customer orders",
                columns=[
                    ColumnSpec(name="order_id", dtype="int", description="Primary key"),
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="amount", dtype="float", description="Order total USD"),
                    ColumnSpec(name="status", dtype="str"),
                    ColumnSpec(name="created_at", dtype="datetime"),
                ],
            ),
            TableSpec(
                name="customers",
                description="Customer accounts",
                columns=[
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="name", dtype="str", pii_risk="low"),
                    ColumnSpec(name="email", dtype="str", pii_risk="high"),
                    ColumnSpec(name="region", dtype="str"),
                ],
            ),
        ],
        join_keys=[
            JoinKey(
                left_table="orders",
                left_column="customer_id",
                right_table="customers",
                right_column="customer_id",
            )
        ],
    )


if __name__ == "__main__":
    from uuid import uuid4

    from secure_query.builder import LQP
    from secure_query.validate import validate_and_compile

    catalog = demo_catalog()
    print("--- catalog sent to LLM planner ---")
    print(catalog.planner_summary())
    print()

    plan = (
        LQP.aggregate(table="orders")
        .join("customers", on=[("orders.customer_id", "customers.customer_id")])
        .filter("customers.region", "eq", "west")
        .group_by_columns(["customers.region"])
        .agg("sum", "orders.amount", alias="total_amount")
        .order_by("total_amount", direction="desc")
        .limit(10)
        .build()
    )
    # Rebuild with fixed flow — builder already set plan_id randomly; fine for demo
    compiled = validate_and_compile(plan, catalog)
    print("--- compiled SQL ---")
    print(compiled.sql)
    print("plan_hash:", compiled.plan_hash[:16], "...")
