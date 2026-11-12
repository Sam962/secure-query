# Secure Query

Governed talk-to-data: **model SQL is validated against an allowlist and regenerated — it never runs as written.**

You ask in English. The model proposes SQL (or picks a catalog-owned metric). Code you own then:

1. **Parses** the SQL and checks every expression against a human-approved **catalog**
2. **Regenerates** the SQL that will run (AST only — no string interpolation of model text)
3. **Executes** read-only, with timeout and row cap
4. **Traces and audits** every ask, confirm, refuse, and execute (hashes + principal; question text is not stored)

Wrong-answer rate is the product metric. A refusal is better than a confident wrong number.

The demo uses the public [Chinook](https://github.com/lerocha/chinook-database) music store (11 tables) on DuckDB. The kernel is schema-agnostic; Chinook is not your production catalog.

New here? Read [docs/DEMO.md](docs/DEMO.md) (why this exists) then come back here to run it.

---

## How to use the model

The default planner (`SECURE_QUERY_PLANNER=sql`) needs an LLM that emits `{"sql": "..."}` JSON.
Set `SECURE_QUERY_PLANNER=lqp` for the LogicalPlan path (metrics / explain helpers).
The default is OpenAI **`gpt-6-luna`** with reasoning off (`SECURE_QUERY_PROVIDER=openai`). It replaced `gpt-4.1-mini`, which OpenAI removes on 2027-01-14, after matching it on the dev suites; the adjudicated Spider number in [docs/STATUS.md](docs/STATUS.md) is still the `gpt-4.1-mini` one. Any model on an OpenAI-compatible endpoint works; measure it first ([docs/EVAL.md](docs/EVAL.md), "Qualifying a new model").
**qwen2.5:7b** via [Ollama](https://ollama.com) is a local option for demos without an API key; it is not what the numbers measure.

### 1. Local demo data

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,planner,api]"
python -m secure_query.demo.load_chinook
```

### 2. Local option: pull an Ollama model

```bash
ollama pull qwen2.5:7b
```

With no `SECURE_QUERY_PROVIDER` and no API key set, a running Ollama server is picked up automatically, with model `qwen2.5:7b` (`SECURE_QUERY_MODEL` / `--model` override it). For the measured setup, set `SECURE_QUERY_PROVIDER=openai` and `OPENAI_API_KEY`.

### 3. Ask from the CLI

```bash
python -m secure_query.demo.ask --provider ollama "revenue by country"
```

`--confirm-only` shows explain-back and compiled SQL without hitting the database.

Pasted SQL and high-PII questions are refused **before** the model is called:

```bash
python -m secure_query.demo.ask --provider ollama "list all customer emails"
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
| `ollama` | Local, no API key | Default model `qwen2.5:7b` |
| `groq` | Hosted, needs `GROQ_API_KEY` | Cloud model ids; do not send an Ollama tag |
| `openai` | Hosted, needs `OPENAI_API_KEY` | Default: `gpt-6-luna`, reasoning off |
| `mock` | Tests / no GPU | Fixed Chinook plan; not a quality measure |

```bash
export SECURE_QUERY_PROVIDER=groq
export GROQ_API_KEY=gsk_...
python -m secure_query.demo.ask --provider groq "revenue by country"
```

### 6. Measure (do not use holdout to tune)

```bash
python -m secure_query.evals.run --split dev --provider openai        # SQL path by default
python -m secure_query.evals.scorecard --suite src/secure_query/evals/suites/chinook/cases.json --sq-only
```

Latest adjudicated result (Spider test 400, the previous default `gpt-4.1-mini`, SQL path with the self-consistency gate): **78.8% right, adjudicated wrong 2.75% (95% CI 1.5–4.9%)**. Details in [docs/STATUS.md](docs/STATUS.md); protocol in [docs/EVAL.md](docs/EVAL.md).

---

## How it works

```
question
  → identity (env / Bearer / SSO header — never from JSON)
  → catalog slice for this principal
  → LLM emits SQL JSON (or approved metric_id)
  → allowlist validate + regenerate (sqlglot AST)  — model SQL never runs as written
  → self-consistency gate (two readings must agree)
  → row filters
  → execute as end-user on Databricks (or DuckDB / local Postgres)
  → signed Evidence Bundle + trace/audit JSONL  (no LLM on result cells)
```

The **catalog** is the security boundary. The database may contain more; anything not listed is unqueryable.

Unity Catalog on Databricks is **not** this catalog. UC is warehouse grants/RLS. Secure Query drafts from UC `information_schema`, then a human approves PII and join keys. Postgres and SharePoint lists join via federation / Lakeflow → Delta ([docs/FEDERATION.md](docs/FEDERATION.md)). See [docs/SECURITY.md](docs/SECURITY.md).

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
| `SECURE_QUERY_BACKEND` | `duckdb` \| `databricks` \| `postgres` (postgres = local CI; prod Postgres via UC federation) |
| `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` | SQL warehouse |
| `SECURE_QUERY_DATABRICKS_AUTH` | `user` (default, end-user token) \| `shared` (demo PAT) |
| `SECURE_QUERY_REVIEW_SECRET` | HMAC for review signatures + Evidence Bundles |
| `SECURE_QUERY_POSTGRES_DSN` / `DATABASE_URL` | Local Postgres only |
| `SECURE_QUERY_RETRIEVE_K` | Prompt table retrieval; `0` = off |
| `GENIE_SPACE_ID` | Head-to-head scorecard (`python -m secure_query.evals.scorecard`) |

`GET /health` and `GET /ready` report the execute backend.

---

## Your own data (not self-serve)

Do **not** auto-go-live from a warehouse URL. Order:

1. Draft a catalog (`python -m secure_query.engine.catalog_draft --duckdb …` or `fetch_unity_catalog_draft`)
2. Owner reviews PII flags and `join_keys` (a strict allowlist; `allow_any_join` is dev-only)
3. Add synonyms / knowledge JSON, and metric definitions under `metrics` in the catalog JSON
4. Fill [docs/OWNERSHIP.md](docs/OWNERSHIP.md)
5. Production auth (`token` or `header`) + SELECT-only warehouse identity
6. Domain holdout, adjudicated (see [docs/EVAL.md](docs/EVAL.md))

---

## Project layout

```
src/secure_query/
  kernel/                 Catalog, SQL allowlist validator, IR, AST compile, trace, evidence (no LLM)
  planner/                LLM clients; validated-SQL planner (default); LogicalPlan planner (metrics)
  engine/                 Execute (DuckDB / Postgres / Databricks), domains, catalog draft
  auth/                   Server-side identity
  api/                    FastAPI + ask pipeline; static/ holds the confirm-first UI
  evals/                  Accuracy harness, Genie scorecard, goldens; suites/{chinook,northwind}/
  demo/                   Chinook + Northwind catalogs, pinned loaders, CLI
tests/                    pytest
config/                   Knowledge + principals templates
docs/                     Architecture, demo, eval, security, ADRs, status, baselines
data/                     Local only (gitignored): downloaded DBs, audit log
```

Dependencies point one way: `api → engine/planner → kernel`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Docs

| Doc | What it is |
|-----|------------|
| [docs/HANDOVER.md](docs/HANDOVER.md) | Start here: setup, request path, rules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Flow, layers, embedding the kernel |
| [docs/DEMO.md](docs/DEMO.md) | Why this exists, one question end-to-end |
| [docs/EVAL.md](docs/EVAL.md) | Dev vs holdout; never tune on holdout |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, identity, Databricks/Postgres |
| [docs/OWNERSHIP.md](docs/OWNERSHIP.md) | Named owner before a real domain |
| [docs/STATUS.md](docs/STATUS.md) | What is measured; changelog |
| [docs/PLAN.md](docs/PLAN.md) | Ordered task list for agents |
| [docs/FEDERATION.md](docs/FEDERATION.md) | Postgres and SharePoint lists via Databricks |
| [docs/adr/001-result-policy.md](docs/adr/001-result-policy.md) | No LLM on result cells |
| [docs/adr/003-validated-sql-planner.md](docs/adr/003-validated-sql-planner.md) | Model SQL never runs as written |
| [docs/adr/005-allowlist-sql-validator.md](docs/adr/005-allowlist-sql-validator.md) | Allowlist policy; SQL is the default NL path |

---

## Development

```bash
pip install -e ".[dev,planner,api,databricks,postgres]"
pytest -q
python -m secure_query.evals.run --split dev --provider mock
```

CI: ruff, pytest, compile SQL grep, mock holdout (exits 1 on any wrong answer).

**Do not commit:** `.env`, `data/`, `*.duckdb`, `config/principals.json`, API tokens.

---

## Status

**Chinook demo:** ready (confirm UI; allowlist SQL path; Evidence Bundle). **Company warehouse:** not ready until you have an approved catalog, owner, end-user Databricks identity, and a domain holdout. Roadmap: [docs/PLAN.md](docs/PLAN.md).

---

## License

MIT — see [LICENSE](LICENSE).
