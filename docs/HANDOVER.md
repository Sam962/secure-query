# Handover — start here

**Updated:** 2026-11-15. Read this file, then [PLAN.md](PLAN.md) (the task list), then
[STATUS.md](STATUS.md) (what is measured).

## What this project is

Governed talk-to-data. A user asks in English; an LLM writes SQL; the kernel parses it,
checks every expression against a human-approved catalog, and **regenerates** the SQL
that runs. Model SQL never runs as written. Every answer or refusal writes a trace record
and returns a signed Evidence Bundle. The goal is to beat Databricks Genie on
adjudicated wrong-rate (PLAN.md, Goal).

## Where things stand (2026-11-15)

- Head-to-head with Genie, 7 Spider test databases, 532 questions, model-rated (gpt-5 plus
  a blind Claude agent): real errors 17 (3.2%, 95% 2.0-5.1%) vs Genie 38 (7.1%, 5.2-9.7%).
  Genie answers more (88% vs 73% on the last four). A human rating of the packets in
  `docs/adjudication/` is still owed before this is quoted.
- Root-cause tracing (Spider dev, BIRD dev; STATUS 2026-11-15) points at the kernel first:
  the unused-join rule rejects bridge and filter joins and its "remove the join" advice
  causes wrong answers; the gate's fingerprint counts kernel-added columns. Sam approved
  P12-P15 on 2026-11-15 (PLAN parking lot); P12 and P13 are committed, P14 and P15 next.


- Branch `trusted-core`, pushed to `origin`, 41 commits ahead of `main`, CI green. No pull
  request is open; Sam decides when to open and merge one.
- PLAN Phases 0, 1, 2 and 4 are done. 653 tests pass.
- **Phase 3 (the live head-to-head with Genie) is the only open phase and is blocked on
  Sam**: task 3.1 needs the domain, the partner date, and Databricks and Genie access. Do
  not start 3.2–3.5 before 3.1.
- Latest number on Spider test 400 (before the 2026-11-15 changes): `gpt-6-luna`, run of 2026-11-13: 15 real errors of 400
  (3.75%, 95% CI 2.3–6.1%), refused 13%. The raters were two models, not people. This
  misses the 2% bar. Details in STATUS.md.
- 2026-11-14 changes are committed locally and **not pushed**: an audit of answers scored
  right, and `SECURE_QUERY_CHECK_MODEL` (a different model as second reader, off by default).

Open, all waiting for Sam:

1. Inputs for 3.1 (above).
2. A human adjudication pass, all in `docs/adjudication/`: the 40 wrong-answer packets of
   2026-11-12, the 2 of 2026-11-13 (`*-new2-packets.json`), the head-to-head packets
   (`genie-h2h-spider-*-packets.json`), and the 60 right answers
   (`*-right60-packets.json`). The last matters most: the model raters flag between 1 and
   11 of the 60.
3. Parking-lot decisions in PLAN.md: P1 (retire the LogicalPlan planner), P4 (certified
   answers), P5 (masking), P6 (conversation), P8 (turn the different-model second reader
   on), P9 (NULL ordering on Databricks).
4. The local branch `backup/trusted-core-before-rewrite` holds the history from before a
   personal detail was removed. Never push it; Sam deletes it when satisfied. Older
   published branches still hold that detail; rewriting them is Sam's decision.

A workspace exists since 2026-11-14: Chinook in `workspace.default` and Spider `aan_1`,
`bakery_1` and `advertising_agencies` each in a schema of that name, each with a Genie
space. Head-to-head on the three Spider databases, 237 questions (STATUS changelog,
2026-11-15): real errors 9 against Genie's 18 by two model raters, intervals overlap, and
Secure Query refused 48 where Genie refused none; with one missing join approved on
`bakery_1`, 10 against 14 and 23 refused. After `no_stored_value` and catalog value lists
(both 2026-11-15), a full re-run gives 6 against 18 on 238 questions, 23 refused; the
intervals still touch and no person has rated. `.env` points at `aan_1`; for another
database set `SECURE_QUERY_DATABRICKS_SCHEMA` and `GENIE_SPACE_ID` for the run
(`scripts/spider_warehouse_load.py` prints both). `.env` has no
`SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN`, so pass the main token for a run. The 2026-11-15
commits are local and not pushed.

Built but never run against the real thing: the Docker image (never built), OIDC sign-in
(no real identity provider), the catalog draft from Unity Catalog, the drift check and
`scripts/databricks_smoke.py`. Check each one live before relying on it.

## Setup (once per machine)

```bash
cd secure-query
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,planner,api,enterprise]"
python -m secure_query.demo.load_chinook      # downloads the pinned demo DB to data/
```

Keep the repo outside `~/Desktop` and `~/Documents`. iCloud Drive syncs those folders and
hides files inside `.venv`, which breaks the editable install.

## The request path (where to look)

| Step | File |
|------|------|
| HTTP + UI | `src/secure_query/api/http.py`, `api/static/index.html` |
| Ask pipeline (one function, `ask`) | `src/secure_query/api/service.py` |
| Identity (dev, token, header, OIDC) and per-user catalog slice | `src/secure_query/auth/__init__.py` |
| Rate limit, metrics, request id | `src/secure_query/api/ops.py`, middleware in `api/http.py` |
| LLM client (any OpenAI-compatible model) | `src/secure_query/planner/llm.py` |
| LLM writes SQL; two-readings gate | `src/secure_query/planner/sql_plan.py` |
| SQL rewrites before validation (value grounding, ranking measure) | `src/secure_query/planner/sql_values.py` |
| Allowlist validator (the security core) | `src/secure_query/kernel/sql_validate.py` |
| Execute (DuckDB / Postgres / Databricks) | `src/secure_query/engine/runtime.py` |
| Trace, fingerprints, Evidence Bundle | `src/secure_query/kernel/trace.py`, `kernel/evidence.py` |
| Signing keys and rotation; hash-chained audit files | `src/secure_query/kernel/signing.py`, `kernel/ledger.py` |
| Catalog approval record and drift check | `src/secure_query/kernel/catalog.py`, `engine/catalog_draft.py` |
| Eval harness and Genie scorecard | `src/secure_query/evals/accuracy.py`, `evals/scorecard.py` |

Layering: `api → engine/planner → kernel`. The kernel never imports the planner or an LLM.

## Model

The default is OpenAI `gpt-6-luna` with reasoning off (`DEFAULT_MODEL` in `planner/llm.py`).
`gpt-4.1-mini`, the model behind the older numbers, is removed by OpenAI on 2027-01-14.
Any model on an OpenAI-compatible endpoint runs; the client drops a parameter the model
rejects. Measure a model before trusting it: [EVAL.md](EVAL.md), "Qualifying a new model".
Live evals need `OPENAI_API_KEY` in `.env` and cost a few dollars per Spider run.

## Production settings

`SECURE_QUERY_ENV=production` refuses to start with dev auth, without a 32-character
signing secret, or with a catalog that has no `approval` record, and turns the per-person
rate limit on. Sign-in, signing, audit and the list of what is sent to the model provider
are in [SECURITY.md](SECURITY.md); every variable is in `.env.example`. The `Dockerfile` is
the production image (it has not been built in this repository's CI yet);
`docker-compose.yml` is the local demo.

## Everyday commands

```bash
export PYTHONPATH=src
pytest -q                                                   # ~7 s, no network
python -m secure_query.demo.ask --provider mock "revenue by country"
python -m secure_query.evals.run --split holdout --provider mock   # SQL path by default
python -m secure_query.evals.scorecard --suite src/secure_query/evals/suites/chinook/cases.json --sq-only --provider mock
python scripts/databricks_smoke.py                          # exits 2 (skip) without credentials
```

## Rules that bite

- Follow PLAN.md in order, one task per change. Ideas go to the PLAN parking lot.
- The agent commits one task at a time, only when the Verify block in PLAN.md is green.
  Push `trusted-core` only when Sam says so (he allowed it on 2026-11-14); never
  force-push. Sam opens pull requests and merges. Never add AI co-author trailers.
- `.env` holds real keys. Never print it or send personal details to an outside service.
- Never tune on holdout cases. Never put dataset or domain words in code.
- Wrong-rate claims are adjudicated. A model's rating is never called "human".
- Tune on dev suites and Spider dev only. Spider test and the holdouts are for one
  confirming run, never for tuning.
- Check a change end to end through the API with a live model, not only through the eval
  harness: on 2026-11-13 the harness ran the consistency gate and the API did not.
- A change to what an answer shows can break the grader. After one, re-check the tie rule
  and the column flags (`extra_columns_ok`, `subset_columns_ok`) on a full run.
- Claude Code enforces some of these by hook (`.claude/hooks/guard.sh`): a force-push, a push
  of the backup branch and an AI co-author line are blocked, and a commit runs ruff, pytest
  and the mock holdout first and is blocked if any fails.
- One working copy per agent: `scripts/agent_worktree.sh <name>` makes `../secure-query-<name>`
  on branch `agent/<name>`, linking `.venv`, `data/` and `.env`. Run with `PYTHONPATH=src` there:
  the shared `.venv` otherwise imports the main checkout's code.
