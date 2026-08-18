"""Approved schema catalog — the security boundary for plan validation.

Human-approved (or eng-approved) allowlist of tables, columns, types, and join keys.
Do not treat LLM-generated "schema understanding" as the sole authority for this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ColumnDtype = Literal["int", "float", "str", "datetime", "bool", "json"]
PiiRisk = Literal["none", "low", "high"]


class ColumnSpec(BaseModel):
    """One allowed column in the catalog."""

    name: str
    dtype: ColumnDtype
    description: str = ""
    pii_risk: PiiRisk = "none"
    is_numeric: bool | None = None  # derived from dtype if omitted
    label_for: str | None = Field(
        default=None,
        description="If this FK column, preferred display column as 'Table.Column' for grouping",
    )

    model_config = ConfigDict(extra="forbid", frozen=True)

    def numeric(self) -> bool:
        if self.is_numeric is not None:
            return self.is_numeric
        return self.dtype in ("int", "float")


class TableSpec(BaseModel):
    """One allowed table."""

    name: str
    description: str = ""
    columns: list[ColumnSpec]
    display_column: str | None = Field(
        default=None,
        description="Preferred human-readable column for this table (e.g. Genre.Name)",
    )

    model_config = ConfigDict(extra="forbid", frozen=True)

    def column_map(self) -> dict[str, ColumnSpec]:
        return {c.name: c for c in self.columns}


class JoinKey(BaseModel):
    """Approved equi-join between two columns."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str

    model_config = ConfigDict(extra="forbid", frozen=True)

    def matches(self, lt: str, lc: str, rt: str, rc: str) -> bool:
        forward = (
            self.left_table == lt
            and self.left_column == lc
            and self.right_table == rt
            and self.right_column == rc
        )
        reverse = (
            self.left_table == rt
            and self.left_column == rc
            and self.right_table == lt
            and self.right_column == lc
        )
        return forward or reverse


class Synonym(BaseModel):
    """Maps a natural-language term to a catalog table or column."""

    term: str
    table_id: str
    column_id: str | None = None
    description: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Catalog(BaseModel):
    """Tenant-scoped allowlist used by validate()."""

    tenant_id: str
    tables: list[TableSpec]
    join_keys: list[JoinKey] = Field(default_factory=list)
    synonyms: list[Synonym] = Field(default_factory=list)
    metric_ids: list[str] = Field(
        default_factory=list,
        description="Ids of approved metrics (see metrics.py); listed in planner_summary",
    )
    max_limit: int = 10000
    require_limit: bool = True
    sql_dialect: str = Field(
        default="duckdb",
        description="sqlglot dialect for compile (duckdb locally, databricks in warehouse)",
    )

    model_config = ConfigDict(extra="forbid", frozen=True)

    def table_map(self) -> dict[str, TableSpec]:
        return {t.name: t for t in self.tables}

    def get_column(self, table_id: str, column_id: str) -> ColumnSpec | None:
        table = self.table_map().get(table_id)
        if table is None:
            return None
        return table.column_map().get(column_id)

    def has_table(self, table_id: str) -> bool:
        return table_id in self.table_map()

    def join_allowed(self, lt: str, lc: str, rt: str, rc: str) -> bool:
        if not self.join_keys:
            # Empty join_keys = allow any equi-join between known columns (dev mode).
            # For production, populate join_keys and this becomes an allowlist.
            return True
        return any(jk.matches(lt, lc, rt, rc) for jk in self.join_keys)

    def synonym_map(self) -> dict[str, list[Synonym]]:
        out: dict[str, list[Synonym]] = {}
        for syn in self.synonyms:
            key = syn.term.lower().strip()
            out.setdefault(key, []).append(syn)
        return out

    def planner_summary(self) -> str:
        """Compact text to send the LLM planner (not raw rows)."""
        lines: list[str] = [f"tenant={self.tenant_id}", "tables:"]
        for table in self.tables:
            label = f"  - {table.name}"
            if table.description:
                label = f"{label}: {table.description}"
            if table.display_column:
                label = f"{label} (prefer group-by label: {table.display_column})"
            lines.append(label)
            for col in table.columns:
                pii = f" [pii={col.pii_risk}]" if col.pii_risk != "none" else ""
                desc = f" — {col.description}" if col.description else ""
                hint = f" → label via {col.label_for}" if col.label_for else ""
                lines.append(f"      {col.name}: {col.dtype}{pii}{desc}{hint}")
        if self.synonyms:
            lines.append("synonyms (use these mappings):")
            for syn in self.synonyms:
                target = syn.table_id if syn.column_id is None else f"{syn.table_id}.{syn.column_id}"
                note = f" — {syn.description}" if syn.description else ""
                lines.append(f"  - {syn.term!r} → {target}{note}")
        if self.metric_ids:
            lines.append("approved_metrics (prefer metric_id when the question matches):")
            for mid in self.metric_ids:
                lines.append(f"  - {mid}")
        if self.join_keys:
            lines.append("approved_joins:")
            for jk in self.join_keys:
                lines.append(
                    f"  - {jk.left_table}.{jk.left_column} = "
                    f"{jk.right_table}.{jk.right_column}"
                )
        return "\n".join(lines)
