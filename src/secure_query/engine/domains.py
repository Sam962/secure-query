"""Named domain catalogs: one approved allowlist per domain.

SECURE_QUERY_DOMAINS=id:path,id2:path2 registers catalog JSON files ('-' or
'sample' means the Chinook demo). SECURE_QUERY_CATALOG_FILE registers
a single domain called "default". Every catalog gets the knowledge overlay and
the SQL dialect of the execute backend.

Only the sample domain imports the demo package, and only when it is loaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from secure_query.kernel.catalog import Catalog
from secure_query.planner.knowledge import overlay_from_env


@dataclass(frozen=True)
class Domain:
    id: str
    catalog_file: str | None = None
    """None = Chinook sample catalog (demo). Path = approved JSON."""
    description: str = ""


def domains_from_env() -> dict[str, Domain]:
    registry = {"chinook": Domain(id="chinook", description="Public Chinook music-store demo")}
    single = (os.environ.get("SECURE_QUERY_CATALOG_FILE") or "").strip()
    if single:
        registry["default"] = Domain(id="default", catalog_file=single)
    for part in (os.environ.get("SECURE_QUERY_DOMAINS") or "").split(","):
        did, sep, path = part.partition(":")
        if not sep or not did.strip():
            continue
        path = path.strip()
        registry[did.strip()] = Domain(
            id=did.strip(), catalog_file=None if path in {"-", "sample"} else path
        )
    return registry


def default_domain_id(registry: dict[str, Domain] | None = None) -> str:
    """SECURE_QUERY_DOMAIN, else the SECURE_QUERY_CATALOG_FILE domain, else Chinook."""
    explicit = (os.environ.get("SECURE_QUERY_DOMAIN") or "").strip()
    if explicit:
        return explicit
    return "default" if "default" in (registry or domains_from_env()) else "chinook"


def load_domain_catalog(
    domain_id: str | None = None, *, dialect: str | None = None
) -> tuple[str, Catalog]:
    """Approved catalog for a domain, with knowledge overlay and backend dialect applied."""
    registry = domains_from_env()
    did = (domain_id or default_domain_id(registry)).strip()
    if did not in registry:
        raise KeyError(f"unknown domain {did!r}; known: {sorted(registry)}")
    domain = registry[did]
    if domain.catalog_file:
        catalog = Catalog.from_json_file(domain.catalog_file)
    else:
        from secure_query.demo.chinook import sample_catalog

        catalog = sample_catalog()
    catalog = overlay_from_env(catalog)
    if dialect and catalog.sql_dialect != dialect:
        catalog = catalog.model_copy(update={"sql_dialect": dialect})
    return did, catalog
