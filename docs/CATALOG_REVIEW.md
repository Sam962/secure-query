# Catalog review: what the data owner writes

The model that writes SQL never sees a row of your data. It sees only the catalog: tables,
columns, joins, and what you write about them. Everything it knows about what your data
*means* comes from this review. We measured it: on BIRD dev with the per-question hints
removed, adding column descriptions won back 91 of the 155 right answers the hints had given.
Three attempts to get the same gain from the prompt alone (more reasoning, guessing the
nearest measure, a written interpretation step) each made answers worse. Write it down here.

An agent may draft the catalog; only the named owner in [OWNERSHIP.md](OWNERSHIP.md) approves it.

## 1. Draft

```bash
# Databricks (DATABRICKS_* and SECURE_QUERY_DATABRICKS_SCHEMA set)
python -m secure_query.engine.catalog_draft --databricks --values --facts -o config/<domain>-catalog.json
# or a DuckDB file
python -m secure_query.engine.catalog_draft --duckdb path/to.duckdb --values --facts -o config/<domain>-catalog.json
```

The draft reads the schema and runs counts only; no stored value leaves the warehouse except
the category codes `--values` lists for you to approve. It fills in:

- tables, columns and types; PII flags from Unity Catalog tags and column names;
- `join_keys`: declared foreign keys plus joins the data shows (printed as `proposed join (review)`);
- descriptions from the comments already in the warehouse (`COMMENT ON` in Unity Catalog or DuckDB);
- with `--values`, the codes of short category columns;
- with `--facts`, notes that start with `Detected:` (see 4).

## 2. Columns and privacy

- Mark every column that identifies or describes a person `pii_risk: "high"`. The kernel then
  refuses any query that reads it, even in a filter or a sort. When unsure, mark it.
- Remove tables and columns no one should ask about. A column not in the catalog cannot be queried.

## 3. Descriptions: say what the name does not

The model already reads the column name. A description that repeats it ("the country of the
plant") adds nothing. Write what someone new to the data would get wrong:

| Write | Example |
|---|---|
| What one row is | "One row per invoice line, not per invoice." |
| Units and scale | "Amount in USD cents." / "Capacity in MW (design net capacity)." |
| What a code means | "Status: A = active, S = suspended, C = closed." |
| What empty means | "NULL end date = still open." |
| Which of two similar columns to use | "Use billing_country for sales by country; customer country can change." |
| Time | "Timestamps are UTC." / "fiscal_year starts in July." |

Table descriptions: one line on what the table holds and its grain. Keep each description
short; the model reads every one on every question.

**Codes must match the data.** Before approving a list of code meanings, check that the codes
appear in that column. One public benchmark's published documentation gives every code
column the same list of US states; a model told that commodity codes are states cannot
find "apples". Wrong documentation is worse than none.

## 4. Detected notes

With `--facts`, the draft adds two kinds of note, from counts only:

- `Detected: not unique: N rows hold M different values; ... group or identify by <key>`:
  the column that names the rows repeats (two cities called Newark, two affiliations with one
  name). Keep the note if the repeats are different things; delete it if they are true duplicates.
- `Detected: every a/b pair is stored twice, once in each direction`: a pair table holds
  both A to B and B to A, so summing every row doubles the total. Keep it if that is how the
  table is meant to be read.

## 5. Business words: synonyms

When people use a word that is not a column name, map it (`synonyms`):

```json
{"term": "headcount", "table_id": "Employee", "description": "staff, all statuses"}
{"term": "region", "table_id": "Customer", "column_id": "Country"}
```

## 6. Measures: metrics

Any number people ask for by name ("revenue", "active customers", "average order value")
and could compute two ways belongs in `metrics`, defined once. The model sees each as its
formula, for example:

```
avg_revenue_per_customer: Total revenue divided by distinct customers
  = SUM(Invoice.Total) / COUNT(DISTINCT Invoice.CustomerId) over Invoice
```

```json
{"id": "total_revenue", "description": "Sum of all invoice totals",
 "source": "Invoice", "aggregations": ["sum:Invoice.Total:total_revenue"], "unit": "USD"}
{"id": "revenue_by_country", "description": "Invoice revenue by customer country",
 "source": "Invoice", "group_by": ["Customer.Country"],
 "aggregations": ["sum:Invoice.Total:revenue"],
 "joins": ["Invoice:Customer:Invoice.CustomerId=Customer.CustomerId"], "unit": "USD"}
{"id": "avg_revenue_per_customer", "description": "Total revenue divided by distinct customers",
 "kind": "ratio", "source": "Invoice",
 "numerator": "sum:Invoice.Total", "denominator": "count_distinct:Invoice.CustomerId", "unit": "USD"}
```

Start with the ten numbers your team reports most. Write the description in the words people
use when they ask.

## 7. Certified answers

For the questions people ask most, approve the exact query (`certified`). A question that asks
the same thing in other words runs your SQL as written, and the answer says it is certified.
On Spider dev, questions whose other wording was certified went from 16 wrong to 4 of 154.

```json
{"id": "customers_by_country", "question": "How many customers per country?",
 "sql": "SELECT Country, COUNT(*) AS customers FROM Customer GROUP BY Country"}
```

Your SQL still passes the same checks as any query (tables, private columns, row filters,
row limit). Write the question the way people ask it; one wording is enough. Someone who may
not read a table never sees a certified answer that reads it.

## 8. House rules: instructions

Short rules that hold for every question, in plain words, never SQL (`instructions`):

```json
["Quantities sold come from InvoiceLine, not Track.",
 "Exclude test accounts: Customer.Company = 'Internal'."]
```

## 9. Do not write

- Stored values beyond the approved category codes: names, emails, amounts. The catalog is sent
  to the model provider with every question.
- Long prose or copied manuals. Every line is read on every question; say the one thing that matters.
- Anything that contradicts the data. A description the data disagrees with turns right answers wrong.

## 10. Approve and keep it current

Add the approval record (`version`, `approved_by`, `approved_at`); production refuses to start
without one. Re-check after schema changes:

```bash
python -m secure_query.engine.catalog_draft --databricks --check config/<domain>-catalog.json
```

It exits 1 when the warehouse no longer matches the approved catalog.
