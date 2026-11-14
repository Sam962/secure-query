# Federation & SharePoint lists

Secure Query’s production dialect surface is **Databricks SQL + Unity Catalog**.
Other systems join that surface; they do not become parallel planners.

## Postgres (Lakehouse Federation)

1. Create a Lakehouse Federation connection with a **SELECT-only** Postgres role.
2. Mount the foreign schemas into a UC catalog/schema.
3. Draft with `fetch_unity_catalog_draft` → owner reviews join keys + PII
   (name-matched sensitive columns default to `high` unless tagged `public` /
   `pii_none`).
4. Set `SECURE_QUERY_BACKEND=databricks`, `SECURE_QUERY_DATABRICKS_SCHEMA`, and
   `SECURE_QUERY_DATABRICKS_AUTH=user`.
5. Keep `SECURE_QUERY_BACKEND=postgres` for local CI only — not the company path.

## SharePoint lists → Delta

1. Ingest **lists** (not documents) with Lakeflow Connect into Delta tables in UC.
2. Same draft → review → approve catalog flow as native UC tables.
3. Document / file Q&A is explicitly out of scope for this product.

## Before treating federated data as production

- [ ] The Lakehouse Federation connection uses a SELECT-only Postgres user.
- [ ] Foreign schemas are mounted in a UC catalog, with RLS and masks set on the UC side.
- [ ] The catalog draft was reviewed by the named owner (join keys and PII).
- [ ] `SECURE_QUERY_BACKEND=databricks`, `SECURE_QUERY_DATABRICKS_SCHEMA` points at the
      federated schema, and `SECURE_QUERY_DATABRICKS_AUTH=user`.
- [ ] `python scripts/databricks_smoke.py` exits 0 against that schema.
