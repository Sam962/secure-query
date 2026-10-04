# Agnostic-planner experiment — 2026-10-04 (WIP, not merged)

Model: qwen2.5:7b @ 845dbda0ea48. Domains: Chinook (133 cases incl. 20 complex) and
clinic (30 cases, synthetic, built for this experiment; see evals/domains/clinic).

| Run | Domain | Correct | Wrong | Answer-rate | Over-refusal |
|-----|--------|---------|-------|-------------|--------------|
| baseline (Chinook-tuned) | clinic | 15/30 | 2 | 35% | 57% |
| guards off (ablation) | clinic | 20/30 | 4 | 65% | 22% |
| guards off (ablation) | chinook | 118/133 | 10 | 95% | 4% |
| redesign bundle | clinic | 17/30 | **8** | 43% | 22% |
| redesign bundle | chinook | 117/133 | **3** | 87% | 11% |

Note: the clinic baseline is partly confounded: its catalog used a bare
`display_column`, which crashed plan normalisation (fixed: catalog now validates
label refs at load).

## Findings
- Without guards the model alone refused every out-of-scope question on Chinook
  (suppliers, payroll, inventory, marketing, shipping). Its errors were PII (5),
  ratio/share/change phrasing (5), and degenerate distinct counts (1).
- The keyword out-of-scope veto refused ordinary words on the unseen schema (11 false
  vs 6 correct), but many of those questions the 7B model answers wrongly, so the
  veto was also hiding errors.
- Shape examples generated from the live catalog were copied into plans (date ranges,
  joins). Examples must not be on the user's own tables.
- Plans add elements the question never asked for: filters (status='completed'),
  group-bys (doctor), date ranges. Prose business rules are over- and under-applied.
- The bundle changed several things at once; effects cannot be attributed. Next
  attempts change one thing per measured run.

## Direction
1. Two-way grounding: every filter literal, group-by column, and join in a plan must
   trace to a question term or a structured catalog definition (reverse of
   dropped_concepts). Deterministic, domain-agnostic.
2. Business rules as structured metrics with filters, not prose instructions.
3. Clinic is no longer a clean holdout (its failures were studied). Split it into
   dev/holdout and add a third small domain for final validation.
