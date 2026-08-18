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

    countries = ["USA", "Canada", "Brazil", "France", "Germany", "UK", "Australia"]
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

    for n in (1, 5, 10, 50, 100):
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
                "id": "dev_invoices_in_2010",
                "question": "How many invoices were issued in 2010?",
                "expect": "answer",
                "reference_sql": (
                    "SELECT COUNT(*) FROM Invoice "
                    "WHERE InvoiceDate >= '2010-01-01' AND InvoiceDate < '2011-01-01'"
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

    for year in range(2009, 2014):
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

    states = ["CA", "NY", "TX", "FL", "WA", "OR", "NV", "AZ", "CO", "MA"]
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

    return cases
