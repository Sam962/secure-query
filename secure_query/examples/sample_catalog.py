"""Approved catalog — Chinook (public demo DB).

The allowlist the planner is permitted to see and `validate` enforces. This is
the security boundary: the DuckDB file may contain more than this, and anything
absent here cannot be queried.

Join keys mirror the source foreign keys, minus `Employee.ReportsTo`, which is
self-referential and cannot be expressed by the IR's single-source join list.
"""

from sqlglot import exp

from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, Synonym, TableSpec
from secure_query.kernel.metrics import MetricSpec, register_builtin


def sample_catalog() -> Catalog:
    return Catalog(
        tenant_id="chinook",
        require_limit=True,
        max_limit=1000,
        tables=[
            TableSpec(
                name="Invoice",
                description="Customer invoices (one row per sale)",
                columns=[
                    ColumnSpec(name="InvoiceId", dtype="int", description="Primary key"),
                    ColumnSpec(name="CustomerId", dtype="int"),
                    ColumnSpec(name="InvoiceDate", dtype="datetime"),
                    ColumnSpec(name="BillingAddress", dtype="str", pii_risk="high"),
                    ColumnSpec(name="BillingCity", dtype="str", pii_risk="low"),
                    ColumnSpec(name="BillingState", dtype="str", pii_risk="low"),
                    ColumnSpec(name="BillingCountry", dtype="str"),
                    ColumnSpec(name="BillingPostalCode", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Total", dtype="float", description="Invoice total", unit="USD"),
                ],
            ),
            TableSpec(
                name="InvoiceLine",
                description="Line items on an invoice (one row per track sold)",
                columns=[
                    ColumnSpec(name="InvoiceLineId", dtype="int", description="Primary key"),
                    ColumnSpec(name="InvoiceId", dtype="int"),
                    ColumnSpec(name="TrackId", dtype="int"),
                    ColumnSpec(name="UnitPrice", dtype="float", unit="USD"),
                    ColumnSpec(name="Quantity", dtype="int"),
                ],
            ),
            TableSpec(
                name="Customer",
                description="Customers",
                columns=[
                    ColumnSpec(name="CustomerId", dtype="int", description="Primary key"),
                    ColumnSpec(name="FirstName", dtype="str", pii_risk="low"),
                    ColumnSpec(name="LastName", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Company", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Address", dtype="str", pii_risk="high"),
                    ColumnSpec(name="City", dtype="str", pii_risk="low"),
                    ColumnSpec(name="State", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Country", dtype="str"),
                    ColumnSpec(name="PostalCode", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Phone", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Fax", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Email", dtype="str", pii_risk="high"),
                    ColumnSpec(
                        name="SupportRepId",
                        dtype="int",
                        description="Employee who supports this customer",
                    ),
                ],
            ),
            TableSpec(
                name="Employee",
                description="Staff, including sales support representatives",
                columns=[
                    ColumnSpec(name="EmployeeId", dtype="int", description="Primary key"),
                    ColumnSpec(name="LastName", dtype="str", pii_risk="low"),
                    ColumnSpec(name="FirstName", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Title", dtype="str", description="Job title"),
                    ColumnSpec(name="ReportsTo", dtype="int", description="Manager EmployeeId"),
                    ColumnSpec(name="BirthDate", dtype="datetime", pii_risk="high"),
                    ColumnSpec(name="HireDate", dtype="datetime"),
                    ColumnSpec(name="Address", dtype="str", pii_risk="high"),
                    ColumnSpec(name="City", dtype="str", pii_risk="low"),
                    ColumnSpec(name="State", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Country", dtype="str"),
                    ColumnSpec(name="PostalCode", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Phone", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Fax", dtype="str", pii_risk="high"),
                    ColumnSpec(name="Email", dtype="str", pii_risk="high"),
                ],
            ),
            TableSpec(
                name="Track",
                description="Individual tracks that can be sold",
                columns=[
                    ColumnSpec(name="TrackId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Name", dtype="str", description="Track title"),
                    ColumnSpec(name="AlbumId", dtype="int"),
                    ColumnSpec(name="MediaTypeId", dtype="int", label_for="MediaType.Name"),
                    ColumnSpec(name="GenreId", dtype="int", label_for="Genre.Name"),
                    ColumnSpec(name="Composer", dtype="str", pii_risk="low"),
                    ColumnSpec(name="Milliseconds", dtype="int", description="Track length"),
                    ColumnSpec(name="Bytes", dtype="int"),
                    ColumnSpec(name="UnitPrice", dtype="float", unit="USD"),
                ],
            ),
            TableSpec(
                name="Album",
                description="Albums",
                columns=[
                    ColumnSpec(name="AlbumId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Title", dtype="str", description="Album title"),
                    ColumnSpec(name="ArtistId", dtype="int", label_for="Artist.Name"),
                ],
            ),
            TableSpec(
                name="Artist",
                description="Recording artists",
                display_column="Artist.Name",
                columns=[
                    ColumnSpec(name="ArtistId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Name", dtype="str", description="Artist name"),
                ],
            ),
            TableSpec(
                name="Genre",
                description="Music genres",
                display_column="Genre.Name",
                columns=[
                    ColumnSpec(name="GenreId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Name", dtype="str", description="Genre name"),
                ],
            ),
            TableSpec(
                name="MediaType",
                description="File/media formats a track is available in",
                display_column="MediaType.Name",
                columns=[
                    ColumnSpec(name="MediaTypeId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Name", dtype="str", description="Media type name"),
                ],
            ),
            TableSpec(
                name="Playlist",
                description="Curated playlists",
                display_column="Playlist.Name",
                columns=[
                    ColumnSpec(name="PlaylistId", dtype="int", description="Primary key"),
                    ColumnSpec(name="Name", dtype="str", description="Playlist name"),
                ],
            ),
            TableSpec(
                name="PlaylistTrack",
                description="Which tracks belong to which playlist",
                columns=[
                    ColumnSpec(name="PlaylistId", dtype="int"),
                    ColumnSpec(name="TrackId", dtype="int"),
                ],
            ),
        ],
        synonyms=[
            Synonym(term="revenue", table_id="Invoice", column_id="Total", description="Invoice revenue"),
            Synonym(term="sales", table_id="Invoice", column_id="Total"),
            Synonym(term="sales", table_id="InvoiceLine", column_id="UnitPrice"),
            Synonym(
                term="revenue",
                table_id="InvoiceLine",
                column_id="UnitPrice",
                description="Line revenue per track sold (Quantity is always 1); use for revenue by track, genre, artist or album",
            ),
            Synonym(term="spend", table_id="Invoice", column_id="Total"),
            Synonym(term="genre", table_id="Genre", column_id="Name"),
            Synonym(term="music genre", table_id="Genre", column_id="Name"),
            Synonym(term="artist", table_id="Artist", column_id="Name"),
            Synonym(term="album", table_id="Album", column_id="Title"),
            Synonym(term="track", table_id="Track", column_id="Name"),
            Synonym(term="playlist", table_id="Playlist", column_id="Name"),
            Synonym(term="media type", table_id="MediaType", column_id="Name"),
            Synonym(term="billing country", table_id="Invoice", column_id="BillingCountry"),
            Synonym(term="customer country", table_id="Customer", column_id="Country"),
            Synonym(term="country", table_id="Customer", column_id="Country"),
            Synonym(term="countries", table_id="Customer", column_id="Country"),
            Synonym(term="quantity", table_id="InvoiceLine", column_id="Quantity"),
            Synonym(term="quantities", table_id="InvoiceLine", column_id="Quantity"),
            Synonym(term="city", table_id="Customer", column_id="City"),
            Synonym(term="state", table_id="Customer", column_id="State"),
            Synonym(term="sold", table_id="InvoiceLine", column_id="TrackId"),
            Synonym(term="employee", table_id="Employee"),
            Synonym(term="employees", table_id="Employee"),
            Synonym(term="headcount", table_id="Employee"),
            Synonym(term="customer", table_id="Customer"),
            Synonym(term="customers", table_id="Customer"),
            Synonym(term="client", table_id="Customer"),
            Synonym(term="clients", table_id="Customer"),
        ],
        metrics=chinook_metrics(),
        instructions=[
            "Tracks sold and quantities sold come from InvoiceLine (one row per track "
            "on an invoice), not Track.",
        ],
        join_keys=[
            JoinKey(
                left_table="Invoice",
                left_column="CustomerId",
                right_table="Customer",
                right_column="CustomerId",
            ),
            JoinKey(
                left_table="InvoiceLine",
                left_column="InvoiceId",
                right_table="Invoice",
                right_column="InvoiceId",
            ),
            JoinKey(
                left_table="InvoiceLine",
                left_column="TrackId",
                right_table="Track",
                right_column="TrackId",
            ),
            JoinKey(
                left_table="Customer",
                left_column="SupportRepId",
                right_table="Employee",
                right_column="EmployeeId",
            ),
            JoinKey(
                left_table="Track",
                left_column="GenreId",
                right_table="Genre",
                right_column="GenreId",
            ),
            JoinKey(
                left_table="Track",
                left_column="AlbumId",
                right_table="Album",
                right_column="AlbumId",
            ),
            JoinKey(
                left_table="Track",
                left_column="MediaTypeId",
                right_table="MediaType",
                right_column="MediaTypeId",
            ),
            JoinKey(
                left_table="Album",
                left_column="ArtistId",
                right_table="Artist",
                right_column="ArtistId",
            ),
            JoinKey(
                left_table="PlaylistTrack",
                left_column="TrackId",
                right_table="Track",
                right_column="TrackId",
            ),
            JoinKey(
                left_table="PlaylistTrack",
                left_column="PlaylistId",
                right_table="Playlist",
                right_column="PlaylistId",
            ),
        ],
    )


# Alias used by README / quick start
def demo_catalog() -> Catalog:
    return sample_catalog()


def _col(table: str, column: str) -> exp.Column:
    return exp.Column(
        this=exp.to_identifier(column, quoted=True),
        table=exp.to_identifier(table, quoted=True),
    )


def _line_item_revenue() -> exp.Select:
    """SUM(UnitPrice * Quantity): the IR has no column arithmetic, so this is a builtin."""
    product = exp.Mul(
        this=_col("InvoiceLine", "UnitPrice"),
        expression=_col("InvoiceLine", "Quantity"),
    )
    total = exp.Anonymous(this="SUM", expressions=[product])
    return (
        exp.Select()
        .select(
            exp.Alias(
                this=total,
                alias=exp.to_identifier("line_item_revenue", quoted=True),
            )
        )
        .from_(exp.Table(this=exp.to_identifier("InvoiceLine", quoted=True)))
    )


register_builtin("line_item_revenue", _line_item_revenue)


def chinook_metrics() -> list[MetricSpec]:
    """Approved Chinook metrics. A real domain ships these in its catalog JSON."""
    return [
        MetricSpec(
            id="total_revenue",
            description="Sum of all invoice totals",
            source="Invoice",
            aggregations=["sum:Invoice.Total:total_revenue"],
            default_limit=1,
            unit="USD",
        ),
        MetricSpec(
            id="invoice_count",
            description="Count of invoices",
            source="Invoice",
            aggregations=["count:*:invoice_count"],
            default_limit=1,
        ),
        MetricSpec(
            id="employee_count",
            description="Count of employees (staff headcount)",
            source="Employee",
            aggregations=["count:*:employee_count"],
            default_limit=1,
        ),
        MetricSpec(
            id="revenue_by_country",
            description="Total invoice revenue grouped by customer country",
            source="Invoice",
            joins=["Invoice:Customer:Invoice.CustomerId=Customer.CustomerId"],
            group_by=["Customer.Country"],
            aggregations=["sum:Invoice.Total:revenue"],
            default_limit=24,
            unit="USD",
        ),
        MetricSpec(
            id="revenue_by_billing_country",
            description="Total invoice revenue grouped by billing country on invoice",
            source="Invoice",
            group_by=["Invoice.BillingCountry"],
            aggregations=["sum:Invoice.Total:revenue"],
            default_limit=24,
            unit="USD",
        ),
        MetricSpec(
            id="revenue_by_genre",
            description="Track sales revenue grouped by genre name",
            source="InvoiceLine",
            joins=[
                "InvoiceLine:Track:InvoiceLine.TrackId=Track.TrackId",
                "Track:Genre:Track.GenreId=Genre.GenreId",
            ],
            group_by=["Genre.Name"],
            aggregations=["sum:InvoiceLine.UnitPrice:revenue"],
            default_limit=24,
            unit="USD",
        ),
        MetricSpec(
            id="avg_revenue_per_customer",
            description="Total revenue divided by distinct customers (ratio metric)",
            kind="ratio",
            source="Invoice",
            numerator="sum:Invoice.Total",
            denominator="count_distinct:Invoice.CustomerId",
            default_limit=1,
            unit="USD",
        ),
        MetricSpec(
            id="line_item_revenue",
            description="Sum of unit price times quantity on invoice lines",
            kind="builtin",
            builder_id="line_item_revenue",
            tables=["InvoiceLine"],
            default_limit=1,
            unit="USD",
        ),
    ]
