# Secure Query

**Governed talk-to-data for sensitive databases — the LLM never writes SQL.**

Natural language goes in; a typed **Logical Query Plan (LQP)** comes out. Your code validates that plan against a human-approved **catalog**, compiles SQL through an AST (no string interpolation), executes read-only with timeouts and row caps, and writes a full **audit trail** (question, plan, explanation, SQL, hashes, principal).

Built for teams that need more than Text2SQL demos: PII blocks, refusal over wrong answers, and replayable audit logs — the transparency Genie and similar tools typically lack.

**New here?** Read [docs/DEMO.md](docs/DEMO.md) for the problem, architecture, and honest limits.

---

## Why not let the LLM write SQL?

| Risk | Text2SQL / Genie-style | Secure Query |
|------|------------------------|--------------|
| SQL injection / prompt tricks | Model outputs executable strings | Model outputs a fixed JSON form only |
| Wrong confident answers | Common | Refusal-first; guards + eval gate |
| PII leakage | Depends on prompts | Column policy + explicit projections |
| Audit / replay | Often opaque | JSONL: plan + SQL + `plan_hash` + `sql_hash` |
| Schema authority | Often auto-discovered dump | Human-approved catalog allowlist |

---

## How it works

```
user question
  → resolve identity (token / SSO header — never from JSON body)
  → slice catalog for this principal (tables, joins, metrics)
  → deterministic guards (PII, out-of-scope terms)
  → LLM emits LogicalPlan JSON or approved metric_id (never SQL)
  → validate(plan, catalog)  — joins, PII, types, no cross joins
  → compile via sqlglot AST
  → inject mandatory row filters
  → execute (DuckDB local | Databricks warehouse)
  → audit JSONL + template answer (no LLM on result rows)
```

The **catalog** (`secure_query/examples/sample_catalog.py` or your own JSON) is the security boundary. The database may contain more; anything not in the catalog is unqueryable.

---

## Quick start (local demo)

Uses public [Chinook](https://github.com/lerocha/chinook-database) data downloaded into `data/` (gitignored).

```bash
git clone <your-repo-url>
cd secure-query
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,planner]"

python -m secure_query.examples.load_sample_db
python -m secure_query.examples.ask_sample --provider mock --confirm-only "revenue by country"
pytest -q
```

**With a local LLM (recommended model for this repo: `qwen2.5:7b`):**

```bash
ollama pull qwen2.5:7b
export SECURE_QUERY_PROVIDER=ollama
export SECURE_QUERY_MODEL=qwen2.5:7b
python -m secure_query.examples.ask_sample --provider ollama "revenue by country"
```

**PII refusal (no model call):**

```bash
python -m secure_query.examples.ask_sample --provider ollama "list all customer emails"
# → clarify: restricted data, attempts: 0
```

---

## HTTP API

```bash
pip install -e ".[dev,planner,api]"
export SECURE_QUERY_AUTH_MODE=dev
export SECURE_QUERY_PRINCIPAL_ID=demo-user
uvicorn secure_query.api:app --reload
```

```bash
curl -s localhost:8000/health
curl -s localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"revenue by country","confirm_only":true}' | python -m json.tool
```

Identity is **never** taken from the request body. See [docs/SECURITY.md](docs/SECURITY.md) and [config/principals.example.json](config/principals.example.json).

---

## Configuration

Copy [`.env.example`](.env.example) to `.env` (never commit `.env`).

| Variable | Purpose |
|----------|---------|
| `SECURE_QUERY_AUTH_MODE` | `dev` \| `token` \| `header` |
| `SECURE_QUERY_CATALOG_FILE` | Approved catalog JSON (default: Chinook sample) |
| `SECURE_QUERY_PRINCIPALS_FILE` | Token → principal mapping |
| `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` | Switch execute to SQL warehouse |
| `SECURE_QUERY_PROVIDER` / `SECURE_QUERY_MODEL` | LLM for planner (Ollama, Groq, OpenAI) |

When all three `DATABRICKS_*` vars are set, `GET /health` returns `"backend": "databricks"`.

---

## Using your own data

The **kernel is schema-agnostic**; the **demo defaults to Chinook**. To point at your warehouse:

1. **Catalog** — draft from Unity Catalog (`secure_query/databricks.py`) or hand-write; data owner approves; set `SECURE_QUERY_CATALOG_FILE`
2. **Metrics** — named measures in `secure_query/metrics.py` (or your registry module)
3. **Evals** — real questions + reference SQL under `secure_query/evals/`
4. **Auth** — production principals in `config/principals.json` (gitignored); use the `.example.json` as template

See [docs/PLAN.md](docs/PLAN.md) and [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Project layout

| Path | Role |
|------|------|
| `secure_query/logical_plan.py` | Frozen Pydantic IR (LQP) |
| `secure_query/catalog.py` | Approved tables, columns, joins, PII flags |
| `secure_query/validate.py` | Trust gate before compile |
| `secure_query/compile.py` | Plan → SQL via sqlglot AST |
| `secure_query/planner.py` | LLM → plan JSON only |
| `secure_query/guard.py` | Deterministic pre/post-plan refusals |
| `secure_query/auth.py` | Principal, catalog slice, row filters |
| `secure_query/execute.py` | DuckDB execute + audit |
| `secure_query/databricks.py` | Warehouse execute + UC catalog draft |
| `secure_query/runtime.py` | Catalog load + backend routing |
| `secure_query/api.py` | FastAPI `/ask` |
| `secure_query/examples/` | Chinook demo + `ask_sample` CLI |
| `secure_query/evals/` | Golden + accuracy harness |
| `docs/` | Plan, security model, demo guide |

---

## Security

- [x] LLM never outputs SQL (`planner.py` + tests)
- [x] Validate before compile; AST-only compile (CI grep gate)
- [x] PII policy, cross-join block, metric allowlist per principal
- [x] Server-side identity; audit includes `principal_id`, `tenant_id`
- [x] Read-only execute, timeout, row cap

Details: [docs/SECURITY.md](docs/SECURITY.md)

**Do not commit:** `.env`, `data/`, `*.duckdb`, `config/principals.json`, real API tokens.

---

## Accuracy (Chinook benchmark)

Measured with **qwen2.5:7b** on 32 questions: **~94% correct, 0% wrong** (wrong → refusal). Small sample; tune on dev set, hold out release eval. Run:

```bash
python -m secure_query.evals.run_chinook --accuracy --provider ollama
```

---

## Development

```bash
pip install -e ".[dev,planner,api,databricks]"
pytest -q
python -m secure_query.evals.run_chinook
```

CI runs on push (`.github/workflows/ci.yml`).

---

## Status

**Trust kernel:** mature (330+ tests). **Production on company data:** requires your catalog, auth config, Databricks wiring, and domain eval gate — see [docs/ROADMAP.md](docs/ROADMAP.md).

---

## License

MIT — see [LICENSE](LICENSE).
