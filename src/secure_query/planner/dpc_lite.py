"""DPC-lite: dual-paradigm consistency without sending row values to the LLM.

Full DPC (arXiv 2604.15163) builds a minimal distinguishing database and a
Python/Pandas twin. Here we stay inside the allowlist:

1. Re-execute the *same* regenerated SQL via the read-only probe (stability).
2. For single-table COUNT(*) / COUNT(DISTINCT col) queries, recompute the count
   with a second regenerated query shape and compare fingerprints.

Disagreement → treat as unstable (caller refuses). Never exposes row values.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.compile import CompiledQuery
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql
from secure_query.kernel.trace import answer_fingerprint

ValueProbe = Callable[[CompiledQuery], Sequence[tuple[Any, ...]]]


def probe_stable(compiled: CompiledQuery, probe: ValueProbe) -> tuple[bool, str, str]:
    """Run the same compiled SQL twice; return (ok, fp1, fp2)."""
    rows1 = list(probe(compiled))
    rows2 = list(probe(compiled))
    fp1, fp2 = answer_fingerprint(rows1), answer_fingerprint(rows2)
    return fp1 == fp2, fp1, fp2


def shadow_count_sql(compiled: CompiledQuery, catalog: Catalog) -> CompiledQuery | None:
    """If `compiled` is a simple COUNT over one catalog table, return an alternate COUNT.

    Alternate uses COUNT(1) vs COUNT(*) — same answer if the plan is a pure row count.
    """
    try:
        tree = sqlglot.parse_one(compiled.sql, read=catalog.sql_dialect)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(tree, exp.Select):
        return None
    if tree.args.get("joins") or tree.args.get("group") or tree.find(exp.Union):
        return None
    aggs = [a for a in tree.find_all(exp.Count) if a.find_ancestor(exp.Select) is tree]
    if len(aggs) != 1:
        return None
    count = aggs[0]
    # COUNT(*) → COUNT(1); anything else → skip
    if not isinstance(count.this, exp.Star):
        return None
    from_ = tree.args.get("from_") or tree.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    # Keep WHERE / QUALIFY so the shadow matches the filtered grain — dropping
    # predicates would compare COUNT(*) of a slice to COUNT(1) of the whole table.
    alt = exp.select(exp.alias_(exp.Count(this=exp.Literal.number(1)), "n", quoted=True)).from_(
        from_.this.copy()
    )
    where = tree.args.get("where")
    if where is not None:
        alt = alt.where(where.this.copy() if hasattr(where, "this") else where.copy())
    qualify = tree.args.get("qualify")
    if qualify is not None:
        alt.set("qualify", qualify.copy())
    alt = alt.limit(catalog.max_limit)
    sql = alt.sql(dialect=catalog.sql_dialect)
    try:
        return validate_sql(sql, catalog).compiled
    except SqlValidationFailed:
        return None


def dpc_lite_check(
    compiled: CompiledQuery,
    catalog: Catalog,
    probe: ValueProbe,
) -> tuple[bool, tuple[str, ...]]:
    """Return (consistent, fingerprints_seen)."""
    ok, fp1, fp2 = probe_stable(compiled, probe)
    fps = [fp1, fp2]
    if not ok:
        return False, tuple(fps)
    shadow = shadow_count_sql(compiled, catalog)
    if shadow is not None:
        try:
            fp3 = answer_fingerprint(list(probe(shadow)))
        except Exception:  # noqa: BLE001 — probe failure is not a consistency pass
            return False, tuple(fps)
        fps.append(fp3)
        if fp3 != fp1:
            return False, tuple(fps)
    return True, tuple(dict.fromkeys(fps))
