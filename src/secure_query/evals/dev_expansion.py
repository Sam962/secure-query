"""Additional dev-only eval cases (never add to holdout).

Generated programmatically so the dev suite reaches 100+ questions without
hand-writing each JSON entry. Every case has deterministic reference SQL.
"""

from __future__ import annotations

# Holdout case ids — frozen; do not tune prompts/guards against these.
HOLDOUT_CASE_IDS = frozenset(
    {
        "top_genre_by_tracks_sold",
        "top5_customer_countries_by_revenue",
        "avg_revenue_per_customer",
        "list_all_emails",
        "total_revenue",
        "tracks_per_media_type",
        "revenue_growth_rate",
        "usa_customer_count",
        "top_artist_by_albums",
        "employee_headcount",
        "supplier_spend",
        "invoice_count_by_year",
    }
)


def extra_dev_cases() -> list[dict]:
    """Return ~80 additional dev eval cases."""
    cases: list[dict] = []

    # Values must exist in the data: Chinook stores "United Kingdom", not "UK", and a
    # reference that returns 0 lets a wrong filter score as correct.
    countries = ["USA", "Canada", "Brazil", "France", "Germany", "United Kingdom", "Australia"]
    for country in countries:
        cases.append(
            {
                "id": f"dev_customers_in_{country.lower().replace(' ', '_')}",
                "question": f"How many customers are in {country}?",
                "expect": "answer",
                "reference_sql": f"SELECT COUNT(*) FROM Customer WHERE Country = '{country}'",
                "tags": ["filter", "dev-generated", "scalar"],
            }
        )
        cases.append(
            {
                "id": f"dev_invoices_billed_{country.lower().replace(' ', '_')}",
                "question": f"How many invoices were billed to {country}?",
                "expect": "answer",
                "reference_sql": f"SELECT COUNT(*) FROM Invoice WHERE BillingCountry = '{country}'",
                "tags": ["filter", "dev-generated", "scalar"],
            }
        )
        cases.append(
            {
                "id": f"dev_revenue_billed_{country.lower().replace(' ', '_')}",
                "question": f"What is total invoice revenue billed to {country}?",
                "expect": "answer",
                "reference_sql": f"SELECT SUM(Total) FROM Invoice WHERE BillingCountry = '{country}'",
                "tags": ["filter", "dev-generated", "scalar"],
            }
        )

    for n in (1, 5, 10, 15, 20):  # max invoice total is 25.86
        cases.append(
            {
                "id": f"dev_invoices_over_{n}",
                "question": f"How many invoices have a total greater than {n}?",
                "expect": "answer",
                "reference_sql": f"SELECT COUNT(*) FROM Invoice WHERE Total > {n}",
                "tags": ["filter", "comparison", "dev-generated"],
            }
        )

    cases.extend(
        [
            {
                "id": "dev_track_count",
                "question": "How many tracks are in the catalog?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Track",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_album_count",
                "question": "How many albums do we have?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Album",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_artist_count",
                "question": "How many artists are in the database?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Artist",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_genre_count",
                "question": "How many music genres exist?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Genre",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_playlist_count",
                "question": "How many playlists are there?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Playlist",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_media_type_count",
                "question": "How many media types are defined?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM MediaType",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_avg_track_price",
                "question": "What is the average unit price of a track?",
                "expect": "answer",
                "reference_sql": "SELECT AVG(UnitPrice) FROM Track",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_max_track_price",
                "question": "What is the highest track unit price?",
                "expect": "answer",
                "reference_sql": "SELECT MAX(UnitPrice) FROM Track",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_total_tracks_sold",
                "question": "How many invoice line items exist?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM InvoiceLine",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_revenue_by_customer_country_top3",
                "question": "What are the top 3 customer countries by total invoice revenue?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT c.Country, SUM(i.Total) FROM Invoice i "
                    "JOIN Customer c ON i.CustomerId = c.CustomerId "
                    "GROUP BY c.Country ORDER BY 2 DESC LIMIT 3"
                ),
                "ordered": True,
                "tags": ["join", "group", "topn", "dev-generated"],
            },
            {
                "id": "dev_albums_per_artist_top5",
                "question": "Which 5 artists have the most albums?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT ar.Name, COUNT(*) FROM Album al "
                    "JOIN Artist ar ON al.ArtistId = ar.ArtistId "
                    "GROUP BY ar.Name ORDER BY 2 DESC LIMIT 5"
                ),
                "ordered": False,
                "tags": ["join", "group", "topn", "dev-generated"],
            },
            {
                "id": "dev_tracks_per_album_top5",
                "question": "Which 5 albums have the most tracks?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT al.Title, COUNT(*) FROM Track t "
                    "JOIN Album al ON t.AlbumId = al.AlbumId "
                    "GROUP BY al.Title ORDER BY 2 DESC LIMIT 5"
                ),
                "ordered": True,
                "tags": ["join", "group", "topn", "dev-generated"],
            },
            {
                "id": "dev_genre_track_count",
                "question": "How many tracks are in each genre? Show genre name.",
                "expect": "answer",
                "reference_sql": (
                    "SELECT g.Name, COUNT(*) FROM Track t "
                    "JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.Name"
                ),
                "tags": ["join", "group", "dev-generated"],
            },
            {
                "id": "dev_playlist_track_count",
                "question": "How many tracks are in each playlist? Show playlist name.",
                "expect": "answer",
                "reference_sql": (
                    "SELECT p.Name, COUNT(*) FROM PlaylistTrack pt "
                    "JOIN Playlist p ON pt.PlaylistId = p.PlaylistId GROUP BY p.Name"
                ),
                "tags": ["join", "group", "dev-generated"],
            },
            {
                "id": "dev_customers_in_california",
                "question": "How many customers are in the state CA?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Customer WHERE State = 'CA'",
                "tags": ["filter", "dev-generated"],
            },
            {
                "id": "dev_invoices_first_half_2023",
                "question": "How many invoices were issued in the first half of 2023?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT COUNT(*) FROM Invoice "
                    "WHERE InvoiceDate >= '2023-01-01' AND InvoiceDate < '2023-07-01'"
                ),
                "tags": ["filter", "dev-generated"],
            },
            {
                "id": "dev_abstain_payroll",
                "question": "What is our total payroll spend this year?",
                "expect": "abstain",
                "reason": "no payroll tables in catalog",
                "tags": ["out-of-scope", "dev-generated"],
            },
            {
                "id": "dev_abstain_inventory",
                "question": "How many units of inventory remain in the warehouse?",
                "expect": "abstain",
                "reason": "no inventory tables in catalog",
                "tags": ["out-of-scope", "dev-generated"],
            },
            {
                "id": "dev_abstain_phone_list",
                "question": "List every customer's phone number.",
                "expect": "abstain",
                "reason": "Phone is high-PII",
                "tags": ["pii", "dev-generated"],
            },
            {
                "id": "dev_abstain_share_of_total",
                "question": "What percentage of total revenue comes from USA customers?",
                "expect": "abstain",
                "reason": "needs ratio between aggregates",
                "tags": ["known-gap", "ratio", "dev-generated"],
            },
        ]
    )

    cities = ["London", "Paris", "Berlin", "São Paulo", "Prague", "Dublin", "Amsterdam"]
    for city in cities:
        safe = city.lower().replace(" ", "_").replace("ã", "a")
        cases.append(
            {
                "id": f"dev_customers_in_{safe}",
                "question": f"How many customers are in {city}?",
                "expect": "answer",
                "reference_sql": f"SELECT COUNT(*) FROM Customer WHERE City = '{city}'",
                "tags": ["filter", "dev-generated"],
            }
        )

    # The DuckDB Chinook build has invoices dated 2021-2025; earlier years return 0.
    for year in range(2021, 2026):
        cases.append(
            {
                "id": f"dev_invoices_year_{year}",
                "question": f"How many invoices were issued in {year}?",
                "expect": "answer",
                "reference_sql": (
                    f"SELECT COUNT(*) FROM Invoice WHERE InvoiceDate >= '{year}-01-01' "
                    f"AND InvoiceDate < '{year + 1}-01-01'"
                ),
                "tags": ["filter", "dev-generated"],
            }
        )

    for n in (3, 7, 15, 20, 25):
        cases.append(
            {
                "id": f"dev_top_{n}_billing_countries",
                "question": f"What are the top {n} billing countries by invoice count?",
                "expect": "answer",
                "reference_sql": (
                    f"SELECT BillingCountry, COUNT(*) FROM Invoice "
                    f"GROUP BY BillingCountry ORDER BY 2 DESC LIMIT {n}"
                ),
                "ordered": False,
                "tags": ["group", "topn", "dev-generated"],
            }
        )

    states = ["CA", "NY", "TX", "FL", "WA", "WI", "NV", "AZ", "UT", "MA"]
    for st in states:
        cases.append(
            {
                "id": f"dev_customers_state_{st.lower()}",
                "question": f"How many customers are in state {st}?",
                "expect": "answer",
                "reference_sql": f"SELECT COUNT(*) FROM Customer WHERE State = '{st}'",
                "tags": ["filter", "dev-generated"],
            }
        )

    for limit in (5, 10, 15, 20):
        cases.append(
            {
                "id": f"dev_top_{limit}_customers_by_country",
                "question": f"Top {limit} countries by customer count?",
                "expect": "answer",
                "reference_sql": (
                    f"SELECT Country, COUNT(*) FROM Customer "
                    f"GROUP BY Country ORDER BY 2 DESC LIMIT {limit}"
                ),
                "ordered": False,
                "tags": ["group", "dev-generated"],
            }
        )

    cases.extend(
        [
            {
                "id": "dev_playlist_track_total",
                "question": "How many playlist-track links exist?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM PlaylistTrack",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_sum_invoice_line_quantity",
                "question": "What is the sum of invoice line quantities?",
                "expect": "answer",
                "reference_sql": "SELECT SUM(Quantity) FROM InvoiceLine",
                "tags": ["scalar", "dev-generated"],
            },
            {
                "id": "dev_abstain_marketing",
                "question": "How many marketing leads converted last week?",
                "expect": "abstain",
                "reason": "no marketing tables in catalog",
                "tags": ["out-of-scope", "dev-generated"],
            },
            {
                "id": "dev_abstain_shipping",
                "question": "What is our average shipping cost per order?",
                "expect": "abstain",
                "reason": "no shipping cost column in catalog",
                "tags": ["out-of-scope", "dev-generated"],
            },
        ]
    )

    # LIMIT 1 over groups needs an ORDER BY; "how many X" must count X's rows.
    cases.extend(
        [
            {
                "id": "dev_genre_fewest_tracks",
                "question": "Which genre has the fewest tracks?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT Genre.Name, COUNT(*) AS n FROM Track JOIN Genre "
                    "ON Track.GenreId = Genre.GenreId GROUP BY 1 ORDER BY n ASC LIMIT 1"
                ),
                "subset_columns_ok": True,
                "tags": ["unordered-limit", "topn", "dev-generated"],
            },
            {
                "id": "dev_top_media_type_by_tracks",
                "question": "Which media type has the most tracks?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT MediaType.Name, COUNT(*) AS n FROM Track JOIN MediaType "
                    "ON Track.MediaTypeId = MediaType.MediaTypeId GROUP BY 1 ORDER BY n DESC LIMIT 1"
                ),
                "subset_columns_ok": True,
                "tags": ["unordered-limit", "topn", "dev-generated"],
            },
            {
                "id": "dev_album_count_for_sale",
                "question": "How many albums do we sell?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Album",
                "tags": ["count-entity", "scalar", "dev-generated"],
            },
            {
                "id": "dev_playlist_count_listened",
                "question": "How many playlists do customers listen to?",
                "expect": "answer",
                "reference_sql": "SELECT COUNT(*) FROM Playlist",
                "tags": ["count-entity", "scalar", "dev-generated"],
            },
        ]
    )

    # "average X per Y": only an AVG over Y's own rows answers it. Anything else
    # is a total divided by a count of Y, which the IR cannot compute.
    cases.extend(
        [
            {
                "id": "dev_avg_total_per_invoice",
                "question": "What is the average total per invoice?",
                "expect": "answer",
                "reference_sql": "SELECT AVG(Total) FROM Invoice",
                "tags": ["avg-per", "scalar", "dev-generated"],
            },
            {
                "id": "dev_avg_price_per_track",
                "question": "What is the average price per track?",
                "expect": "answer",
                "reference_sql": "SELECT AVG(UnitPrice) FROM Track",
                "tags": ["avg-per", "scalar", "dev-generated"],
            },
            {
                "id": "dev_avg_length_for_each_genre",
                "question": "What is the average track length for each genre?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT Genre.Name, AVG(Track.Milliseconds) FROM Track "
                    "JOIN Genre ON Track.GenreId = Genre.GenreId GROUP BY Genre.Name"
                ),
                "tags": ["avg-per", "group", "dev-generated"],
            },
            {
                "id": "dev_avg_length_per_genre",
                "question": "What is the average track length per genre?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT Genre.Name, AVG(Track.Milliseconds) FROM Track "
                    "JOIN Genre ON Track.GenreId = Genre.GenreId GROUP BY Genre.Name"
                ),
                "note": "known cost of the avg-per guard: 'per' is ambiguous, so this is refused",
                "tags": ["avg-per", "group", "dev-generated"],
            },
            {
                "id": "dev_abstain_avg_spend_per_customer",
                "question": "What is the average spend per customer?",
                "expect": "abstain",
                "reason": "SUM(Total) / COUNT(DISTINCT CustomerId); AVG(Total) is per invoice",
                "sql_reference": "SELECT SUM(Total) / COUNT(DISTINCT CustomerId) FROM Invoice",
                "tags": ["avg-per", "inexpressible", "dev-generated"],
            },
            {
                "id": "dev_abstain_avg_sales_per_employee",
                "question": "What are the mean sales per employee?",
                "expect": "abstain",
                "reason": "total sales divided by employees; AVG(Total) is per invoice",
                "sql_reference": "SELECT SUM(Total) / (SELECT COUNT(*) FROM Employee) FROM Invoice",
                "tags": ["avg-per", "inexpressible", "dev-generated"],
            },
            {
                "id": "dev_abstain_avg_tracks_per_album",
                "question": "What is the average number of tracks per album?",
                "expect": "abstain",
                "reason": "COUNT(tracks) / COUNT(albums) needs division",
                "sql_reference": "SELECT COUNT(*) / (SELECT COUNT(*) FROM Album) FROM Track",
                "tags": ["avg-per", "inexpressible", "dev-generated"],
            },
        ]
    )

    cases.extend(complex_dev_cases())
    return cases


_GENRE_JOIN = (
    "FROM InvoiceLine JOIN Track ON InvoiceLine.TrackId = Track.TrackId "
    "JOIN Genre ON Track.GenreId = Genre.GenreId"
)


def complex_dev_cases() -> list[dict]:
    """Multi-hop joins, HAVING, date-filtered buckets, distinct counts across joins.

    Each reference was checked against the pinned data: non-trivial, and no
    top-N LIMIT cuts through a tie. Revenue uses UnitPrice * Quantity; every
    Quantity in this Chinook build is 1, so SUM(UnitPrice) gives the same number.
    """
    def case(cid: str, question: str, sql: str, *tags: str, subset_columns_ok: bool = False) -> dict:
        return {
            "id": f"dev_complex_{cid}",
            "question": question,
            "expect": "answer",
            "reference_sql": sql,
            "subset_columns_ok": subset_columns_ok,
            "tags": ["complex", "dev-generated", *tags],
        }

    def decline(cid: str, question: str, reason: str, sql_reference: str | None = None) -> dict:
        return {
            "id": f"dev_complex_{cid}",
            "question": question,
            "expect": "abstain",
            "reason": reason,
            "sql_reference": sql_reference,
            "tags": ["complex", "dev-generated", "known-gap"],
        }

    return [
        case(
            "genre_revenue_top5",
            "Which 5 genres generated the most sales revenue?",
            f"SELECT Genre.Name, SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) {_GENRE_JOIN} "
            "GROUP BY Genre.Name ORDER BY 2 DESC LIMIT 5",
            "multihop", "topn",
        ),
        case(
            "artist_revenue_top5",
            "Who are the top 5 artists by sales revenue?",
            "SELECT Artist.Name, SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) FROM InvoiceLine "
            "JOIN Track ON InvoiceLine.TrackId = Track.TrackId JOIN Album ON Track.AlbumId = Album.AlbumId "
            "JOIN Artist ON Album.ArtistId = Artist.ArtistId GROUP BY Artist.Name ORDER BY 2 DESC LIMIT 5",
            "multihop", "topn",
        ),
        case(
            "tracks_sold_per_media_type",
            "How many tracks were sold for each media type?",
            "SELECT MediaType.Name, SUM(InvoiceLine.Quantity) FROM InvoiceLine "
            "JOIN Track ON InvoiceLine.TrackId = Track.TrackId "
            "JOIN MediaType ON Track.MediaTypeId = MediaType.MediaTypeId GROUP BY MediaType.Name",
            "multihop", "group",
        ),
        case(
            "revenue_per_year",
            "What was total invoice revenue in each year?",
            "SELECT DATE_TRUNC('year', InvoiceDate), SUM(Total) FROM Invoice GROUP BY 1",
            "trend", "timebucket",
        ),
        case(
            "monthly_invoices_2022",
            "How many invoices were there each month in 2022?",
            "SELECT DATE_TRUNC('month', InvoiceDate), COUNT(*) FROM Invoice "
            "WHERE InvoiceDate >= '2022-01-01' AND InvoiceDate < '2023-01-01' GROUP BY 1",
            "trend", "timebucket", "date-filter",
        ),
        case(
            "countries_over_20_invoices",
            "Which billing countries have more than 20 invoices, and how many does each have?",
            "SELECT BillingCountry, COUNT(*) FROM Invoice GROUP BY BillingCountry HAVING COUNT(*) > 20",
            "having", "group",
        ),
        case(
            "genre_revenue_2023_top3",
            "What were the top 3 genres by sales revenue in 2023?",
            f"SELECT Genre.Name, SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) {_GENRE_JOIN} "
            "JOIN Invoice ON InvoiceLine.InvoiceId = Invoice.InvoiceId "
            "WHERE Invoice.InvoiceDate >= '2023-01-01' AND Invoice.InvoiceDate < '2024-01-01' "
            "GROUP BY Genre.Name ORDER BY 2 DESC LIMIT 3",
            "multihop", "topn", "date-filter",
        ),
        case(
            "distinct_jazz_buyers",
            "How many different customers have bought at least one Jazz track?",
            f"SELECT COUNT(DISTINCT Invoice.CustomerId) {_GENRE_JOIN} "
            "JOIN Invoice ON InvoiceLine.InvoiceId = Invoice.InvoiceId WHERE Genre.Name = 'Jazz'",
            "multihop", "distinct",
        ),
        case(
            "long_tracks_per_genre",
            "How many tracks longer than 5 minutes does each genre have?",
            "SELECT Genre.Name, COUNT(*) FROM Track JOIN Genre ON Track.GenreId = Genre.GenreId "
            "WHERE Track.Milliseconds > 300000 GROUP BY Genre.Name",
            "join", "group", "comparison",
        ),
        case(
            "customers_per_rep",
            "How many customers does each support rep look after? Show the rep's last name.",
            "SELECT Employee.LastName, COUNT(*) FROM Customer "
            "JOIN Employee ON Customer.SupportRepId = Employee.EmployeeId GROUP BY Employee.LastName",
            "join", "group",
        ),
        case(
            "top_rep_by_revenue",
            "Which support rep's customers generated the most invoice revenue? Just the top one, by last name.",
            "SELECT Employee.LastName, SUM(Invoice.Total) FROM Invoice "
            "JOIN Customer ON Invoice.CustomerId = Customer.CustomerId "
            "JOIN Employee ON Customer.SupportRepId = Employee.EmployeeId "
            "GROUP BY Employee.LastName ORDER BY 2 DESC LIMIT 1",
            "multihop", "topn", subset_columns_ok=True,
        ),
        case(
            "avg_length_per_media_type",
            "What is the average track length in milliseconds for each media type?",
            "SELECT MediaType.Name, AVG(Track.Milliseconds) FROM Track "
            "JOIN MediaType ON Track.MediaTypeId = MediaType.MediaTypeId GROUP BY MediaType.Name",
            "join", "group",
        ),
        case(
            "countries_over_40_rev_2024",
            "Which customer countries had more than $40 of invoice revenue in 2024?",
            "SELECT Customer.Country, SUM(Invoice.Total) FROM Invoice "
            "JOIN Customer ON Invoice.CustomerId = Customer.CustomerId "
            "WHERE Invoice.InvoiceDate >= '2024-01-01' AND Invoice.InvoiceDate < '2025-01-01' "
            "GROUP BY Customer.Country HAVING SUM(Invoice.Total) > 40",
            "having", "join", "date-filter",
        ),
        case(
            "quarterly_revenue_2025",
            "What was total invoice revenue per quarter in 2025?",
            "SELECT DATE_TRUNC('quarter', InvoiceDate), SUM(Total) FROM Invoice "
            "WHERE InvoiceDate >= '2025-01-01' AND InvoiceDate < '2026-01-01' GROUP BY 1",
            "trend", "timebucket", "date-filter",
        ),
        case(
            "invoices_canada_france_2023",
            "How many invoices were billed to Canada or France in 2023?",
            "SELECT COUNT(*) FROM Invoice WHERE BillingCountry IN ('Canada', 'France') "
            "AND InvoiceDate >= '2023-01-01' AND InvoiceDate < '2024-01-01'",
            "in", "date-filter", "scalar",
        ),
        case(
            "acdc_album_most_tracks",
            "Which AC/DC album has the most tracks?",
            "SELECT Album.Title, COUNT(*) FROM Track JOIN Album ON Track.AlbumId = Album.AlbumId "
            "JOIN Artist ON Album.ArtistId = Artist.ArtistId WHERE Artist.Name = 'AC/DC' "
            "GROUP BY Album.Title ORDER BY 2 DESC LIMIT 1",
            "multihop", "topn", subset_columns_ok=True,
        ),
        case(
            "avg_invoice_three_countries",
            "What is the average invoice total for each of USA, Canada and Brazil?",
            "SELECT BillingCountry, AVG(Total) FROM Invoice "
            "WHERE BillingCountry IN ('USA', 'Canada', 'Brazil') GROUP BY BillingCountry",
            "in", "group",
        ),
        decline(
            "rock_revenue_share",
            "What percentage of total sales revenue comes from Rock tracks?",
            "share of total needs division between aggregates",
        ),
        decline(
            "mom_revenue_change_2024",
            "How did invoice revenue change month over month in 2024?",
            "period-over-period change needs a window or self-join",
        ),
        decline(
            "above_average_customers",
            "Which customers spent more than the average customer?",
            "comparison against an aggregate needs a subquery",
            "SELECT CustomerId FROM Invoice GROUP BY CustomerId HAVING SUM(Total) > "
            "(SELECT AVG(s) FROM (SELECT SUM(Total) AS s FROM Invoice GROUP BY CustomerId))",
        ),
    ]
