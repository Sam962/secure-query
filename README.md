# Secure Query

Governed talk-to-data: **the LLM never writes SQL.**

You ask in English. The model fills in a typed **Logical Query Plan (LQP)** — a small JSON form of tables, joins, filters, and aggregates. Code you own then:

1. **Validates** the plan against a human-approved **catalog** (not a live schema dump)
2. **Compiles** SQL with sqlglot (AST only — no string interpolation)
3. **Executes** read-only, with timeout and row cap
4. **Audits** the question, plan, SQL, hashes, and principal

Wrong-answer rate is the product metric. A refusal is better than a confident wrong number.

The demo uses the public [Chinook](https://github.com/lerocha/chinook-database) music store (11 tables) on DuckDB. The kernel is schema-agnostic; Chinook is not your production catalog.

New here? Read [docs/DEMO.md](docs/DEMO.md) (why this exists) then come back here to run it.

---

## How to use the model

The planner needs an LLM that can emit **LogicalPlan JSON**, not SQL. This repo is measured on **qwen2.5:7b** via [Ollama](https://ollama.com). Smaller tags such as `llama3.2` will look like a kernel regression (they are not).

### 1. Local demo data

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,planner,api]"
python -m secure_query.examples.load_sample_db
```

### 2. Pull the recommended model

```bash
ollama pull qwen2.5:7b
```

Ollama is the default provider when it is running. The default model is `qwen2.5:7b` (`SECURE_QUERY_MODEL` / `--model` override it).

### 3. Ask from the CLI

```bash
python -m secure_query.examples.ask_sample --provider ollama "revenue by country"
```

`--confirm-only` shows explain-back and compiled SQL without hitting the database.

Pasted SQL and high-PII questions are refused **before** the model is called:

```bash
python -m secure_query.examples.ask_sample --provider ollama "list all customer emails"
```

If the question cannot be planned, the CLI may print **catalog suggestions**. Those are approved phrasings you can type next — the original question is never silently rewritten. Review still required.

### 4. UI (confirm, then run)

```bash
export SECURE_QUERY_PROVIDER=ollama
uvicorn secure_query.api:app --reload
```

Open http://localhost:8000/ — **Review plan**, then **Run query**. The Bearer token field is only for `AUTH_MODE=token` (unused in local `dev`).

```bash
docker compose up --build   # API + Chinook demo
```

### 5. Other providers

| Provider | When | Notes |
|----------|------|--------|
| `ollama` | Local (recommended) | Default model `qwen2.5:7b` |
| `groq` | Hosted, needs `GROQ_API_KEY` | Cloud model ids; do not send an Ollama tag |
| `openai` | Hosted, needs `OPENAI_API_KEY` | |
| `mock` | Tests / no GPU | Fixed Chinook plan; not a quality measure |

```bash
export SECURE_QUERY_PROVIDER=groq
export GROQ_API_KEY=gsk_...
python -m secure_query.examples.ask_sample --provider groq "revenue by country"
```

### 6. Measure (do not use holdout to tune)

```bash
python -m secure_query.evals.run_chinook --accuracy --split holdout --fail-on-wrong --provider ollama
```

Latest local gate (2026-10-03, `qwen2.5:7b` re-pulled 2026-09-07): **10/12, 2 wrong — failing** (model drift; see [docs/STATUS.md](docs/STATUS.md)). The 2026-08-18 build scored 12/12, 0% wrong. Four of those twelve are expected refusals (PII, missing tables, ratios the IR cannot say) and count as correct when the kernel declines.

Protocol: [docs/EVAL.md](docs/EVAL.md).

---

## How it works

```
question
  → identity (env / Bearer / SSO header — never from JSON)
  → catalog slice for this principal
  → guards (PII, out-of-scope terms)
  → LLM emits LogicalPlan JSON or approved metric_id
  → validate(plan, catalog)
  → compile (sqlglot AST)
  → row filters
  → execute (DuckDB | Databricks | Postgres)
  → template answer + audit JSONL  (no LLM on result cells)
```

The **catalog** is the security boundary. The database may contain more; anything not listed is unqueryable.

Unity Catalog on Databricks is **not** this catalog. UC is warehouse grants/RLS. Secure Query drafts from UC `information_schema`, then a human approves PII and join keys. See [docs/SECURITY.md](docs/SECURITY.md).

---

## Configuration

Copy [`.env.example`](.env.example) to `.env`. Never commit `.env`.

| Variable | Purpose |
|----------|---------|
| `SECURE_QUERY_PROVIDER` / `SECURE_QUERY_MODEL` | Planner LLM |
| `SECURE_QUERY_AUTH_MODE` | `dev` \| `token` \| `header` |
| `SECURE_QUERY_ENV` | `production` forbids `AUTH_MODE=dev` |
| `SECURE_QUERY_CATALOG_FILE` | Approved catalog JSON (default: Chinook sample) |
| `SECURE_QUERY_KNOWLEDGE_FILE` | Extra synonyms + instructions (never SQL examples) |
| `SECURE_QUERY_PRINCIPALS_FILE` | Token → principal map |
| `SECURE_QUERY_BACKEND` | `duckdb` \| `databricks` \| `postgres` |
| `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` | SQL warehouse |
| `SECURE_QUERY_POSTGRES_DSN` / `DATABASE_URL` | Postgres |
| `SECURE_QUERY_RETRIEVE_K` | Prompt table retrieval; `0` = off |

`GET /health` and `GET /ready` report the execute backend.

---

## Your own data (not self-serve)

Do **not** auto-go-live from a warehouse URL. Order:

1. Draft a catalog (`python -m secure_query.engine.catalog_draft --duckdb …` or `fetch_unity_catalog_draft`)
2. Owner reviews PII flags and `join_keys` (a strict allowlist; `allow_any_join` is dev-only)
3. Add synonyms / knowledge JSON, and metric definitions under `metrics` in the catalog JSON
4. Fill [docs/OWNERSHIP.md](docs/OWNERSHIP.md)
5. Production auth (`token` or `header`) + SELECT-only warehouse identity
6. Domain holdout with wrong-rate = 0

---

## Project layout

```
frontend/                 Confirm-first UI
secure_query/
  kernel/                 IR, catalog, validate, AST compile, explain
  planner/                NL → LogicalPlan (never SQL)
  engine/                 DuckDB / Postgres / Databricks + catalog draft
  auth/                   Server-side identity
  api/                    FastAPI + ask pipeline
  examples/               Chinook demo + CLI
  evals/                  Goldens + accuracy
config/                   Knowledge + principals templates
docs/                     Demo, eval, security, ADRs, status
```

---

## Docs

| Doc | What it is |
|-----|------------|
| [docs/DEMO.md](docs/DEMO.md) | Why LQP, one question end-to-end |
| [docs/EVAL.md](docs/EVAL.md) | Dev vs holdout; never tune on holdout |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, identity, Databricks/Postgres |
| [docs/OWNERSHIP.md](docs/OWNERSHIP.md) | Named owner before a real domain |
| [docs/STATUS.md](docs/STATUS.md) | What is done; what to do next |
| [docs/adr/001-result-policy.md](docs/adr/001-result-policy.md) | No LLM on result cells |
| [docs/adr/002-ir-boundary.md](docs/adr/002-ir-boundary.md) | LQP will not become a SQL AST |

---

## Development

```bash
pip install -e ".[dev,planner,api,databricks,postgres]"
pytest -q
python -m secure_query.evals.run_chinook
```

CI: pytest, compile SQL grep, mock holdout `--fail-on-wrong`.

**Do not commit:** `.env`, `data/`, `*.duckdb`, `config/principals.json`, API tokens.

---

## Status

**Chinook demo:** runs end to end (confirm UI), but the live holdout gate currently fails on the re-pulled qwen2.5:7b (10/12, 2 wrong). **Company warehouse:** not ready until you have an approved catalog, owner, production auth, and a domain holdout. Details in [docs/STATUS.md](docs/STATUS.md).

---

## License

MIT — see [LICENSE](LICENSE).
