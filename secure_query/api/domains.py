"""Named domain catalogs. Each domain is an isolated allowlist + eval suite."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from secure_query.kernel.catalog import Catalog
from secure_query.engine.databricks import catalog_json_path
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner.knowledge import overlay_from_env


@dataclass(frozen=True)
class Domain:
    """One production domain: catalog source + optional eval split name."""

    id: str
    catalog_file: str | None = None
    """None = Chinook sample catalog (demo). Path = approved JSON."""

    description: str = ""


def builtin_domains() -> dict[str, Domain]:
    return {
        "chinook": Domain(
            id="chinook",
            catalog_file=None,
            description="Public Chinook music-store demo",
        ),
    }


def domains_from_env() -> dict[str, Domain]:
    """SECURE_QUERY_DOMAINS=id:path,id2:path2  (path '-' means sample catalog)."""
    registry = builtin_domains()
    raw = (os.environ.get("SECURE_QUERY_DOMAINS") or "").strip()
    if not raw:
        extra = (os.environ.get("SECURE_QUERY_CATALOG_FILE") or "").strip()
        if extra:
            registry = dict(registry)
            registry["default"] = Domain(id="default", catalog_file=extra)
        return registry
    registry = dict(registry)
    for part in raw.split(","):
        if ":" not in part:
            continue
        did, path = part.split(":", 1)
        did = did.strip()
        path = path.strip()
        registry[did] = Domain(
            id=did,
            catalog_file=None if path in {"-", "sample"} else path,
        )
    return registry


def default_domain_id(registry: dict[str, Domain] | None = None) -> str:
    explicit = (os.environ.get("SECURE_QUERY_DOMAIN") or "").strip()
    if explicit:
        return explicit
    names = list((registry or domains_from_env()).keys())
    if "chinook" in names:
        return "chinook"
    return names[0]


def load_domain_catalog(domain_id: str | None = None) -> tuple[str, Catalog]:
    """Load the approved catalog for a domain, then apply knowledge overlay."""
    registry = domains_from_env()
    did = (domain_id or default_domain_id(registry)).strip()
    if did not in registry:
        raise KeyError(f"unknown domain {did!r}; known: {sorted(registry)}")
    domain = registry[did]
    if domain.catalog_file:
        catalog = catalog_json_path(Path(domain.catalog_file))
    else:
        catalog = sample_catalog()
    return did, overlay_from_env(catalog)
