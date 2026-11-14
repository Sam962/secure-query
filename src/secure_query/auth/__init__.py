"""Request-scoped authorization — catalog slice + mandatory row predicates.

Identity is resolved server-side from env / bearer tokens / trusted SSO headers.
Callers must never send principal_id, tenant_id, or allowed_tables in the body.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import Filter, LogicalPlan
from secure_query.kernel.metrics import metric_tables


class AuthError(Exception):
    """Raised when identity cannot be resolved or is not authorized."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthConfigError(RuntimeError):
    """The auth configuration is unsafe; the service must not start."""


AuthMode = Literal["dev", "token", "header", "jwt"]


@dataclass(frozen=True)
class Principal:
    """Authenticated caller. Drives catalog filtering and row-level predicates."""

    principal_id: str
    tenant_id: str
    allowed_tables: frozenset[str] | None = None
    """None = all tables in base catalog (dev/admin only); else subset."""

    row_filters: tuple[Filter, ...] = ()
    """Predicates injected at compile time; plan cannot remove them."""

    warehouse_token: str | None = None
    """End-user Databricks SQL / OAuth token for on-behalf-of warehouse execute."""


def catalog_for_principal(base: Catalog, principal: Principal) -> Catalog:
    """Return a catalog slice visible to this principal, including metrics."""
    if principal.tenant_id != base.tenant_id:
        raise PermissionError(
            f"principal tenant {principal.tenant_id!r} != catalog tenant {base.tenant_id!r}"
        )
    if principal.allowed_tables is None:
        return base
    allowed = principal.allowed_tables
    tables = [t for t in base.tables if t.name in allowed]
    visible = {t.name for t in tables}
    join_keys = [
        jk
        for jk in base.join_keys
        if jk.left_table in allowed and jk.right_table in allowed
    ]
    return Catalog(
        tenant_id=base.tenant_id,
        tables=tables,
        join_keys=join_keys,
        allow_any_join=base.allow_any_join,
        synonyms=[s for s in base.synonyms if s.table_id in allowed],
        metrics=[m for m in base.metrics if metric_tables(m) <= visible],
        instructions=list(base.instructions),
        max_limit=base.max_limit,
        require_limit=base.require_limit,
        sql_dialect=base.sql_dialect,
    )


def inject_row_filters(plan: LogicalPlan, principal: Principal) -> LogicalPlan:
    """Return plan with mandatory row filters prepended (immutable)."""
    if not principal.row_filters:
        return plan
    merged = list(principal.row_filters) + list(plan.filters)
    return plan.model_copy(update={"filters": merged})


def assert_builtin_metric_allowed(principal: Principal) -> None:
    """Builtin metrics compile SQL without a plan, so row filters cannot be injected."""
    if principal.row_filters:
        raise PermissionError(
            "builtin metrics cannot run when the principal has mandatory row filters"
        )


def audit_principal_fields(principal: Principal | None) -> dict[str, str | None]:
    if principal is None:
        return {"principal_id": None, "tenant_id": None}
    return {"principal_id": principal.principal_id, "tenant_id": principal.tenant_id}


def auth_mode() -> AuthMode:
    explicit = (os.environ.get("SECURE_QUERY_AUTH_MODE") or "").strip().lower()
    if explicit in ("dev", "token", "header", "jwt"):
        return explicit  # type: ignore[return-value]
    if (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower() == "production":
        return "token"
    return "dev"


def trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """SECURE_QUERY_TRUSTED_PROXIES: comma-separated IPs / CIDRs allowed to set identity headers."""
    raw = os.environ.get("SECURE_QUERY_TRUSTED_PROXIES") or ""
    try:
        return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise AuthConfigError(f"SECURE_QUERY_TRUSTED_PROXIES is invalid: {exc}") from exc


def check_auth_config() -> None:
    """Fail closed at startup: header mode needs a proxy allowlist; production needs a signing secret."""
    if auth_mode() == "header" and not trusted_proxies():
        raise AuthConfigError(
            "SECURE_QUERY_AUTH_MODE=header requires SECURE_QUERY_TRUSTED_PROXIES "
            "(the SSO proxy addresses allowed to set identity headers)"
        )
    if auth_mode() == "jwt":
        missing = [name for name in ("SECURE_QUERY_OIDC_ISSUER", "SECURE_QUERY_OIDC_AUDIENCE") if not _env(name)]
        if missing:
            raise AuthConfigError(f"SECURE_QUERY_AUTH_MODE=jwt requires {' and '.join(missing)}")
    production = (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower() == "production"
    if production and len((os.environ.get("SECURE_QUERY_REVIEW_SECRET") or "").strip()) < 32:
        raise AuthConfigError(
            "SECURE_QUERY_ENV=production requires SECURE_QUERY_REVIEW_SECRET of at least 32 "
            "characters; without it each process signs with a random key and Evidence Bundles "
            "stop verifying after a restart or on another worker"
        )


def _from_trusted_proxy(client_host: str | None) -> bool:
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(address in network for network in trusted_proxies())


def resolve_principal(
    *,
    authorization: str | None = None,
    forwarded_user: str | None = None,
    settings: dict[str, Any] | None = None,
    client_host: str | None = None,
    warehouse_token: str | None = None,
) -> Principal:
    """Resolve the caller. Never reads identity from a JSON body.

    `warehouse_token` is the end-user Databricks credential forwarded by Apps /
    OAuth (e.g. X-Databricks-Access-Token). It is never taken from JSON.
    """
    mode = auth_mode()
    registry = settings if settings is not None else principal_registry()
    tenant_id = str(registry.get("tenant_id") or os.environ.get("SECURE_QUERY_TENANT_ID") or "chinook")
    principals: dict[str, Any] = registry.get("principals") or {}
    user_wh_token = (warehouse_token or "").strip() or None

    if mode == "dev":
        principal_id = (
            os.environ.get("SECURE_QUERY_PRINCIPAL_ID") or "demo-user"
        ).strip()
        return _principal_from_registry(
            principal_id,
            tenant_id,
            principals,
            allow_missing=True,
            warehouse_token=user_wh_token,
        )

    if mode == "token":
        token = _bearer_token(authorization)
        token_map: dict[str, str] = registry.get("tokens") or {}
        principal_id = token_map.get(token)
        if not principal_id:
            raise AuthError("invalid or missing API token", status_code=401)
        return _principal_from_registry(
            principal_id,
            tenant_id,
            principals,
            allow_missing=False,
            warehouse_token=user_wh_token,
        )

    if mode == "jwt":
        claims = _jwt_claims(_bearer_token(authorization))
        subject = str(claims.get(_env("SECURE_QUERY_OIDC_SUBJECT_CLAIM") or "sub") or "").strip()
        if not subject:
            raise AuthError("token has no subject", status_code=401)
        groups = claims.get(_env("SECURE_QUERY_OIDC_GROUPS_CLAIM") or "groups") or []
        if isinstance(groups, str):
            groups = [groups]
        return _principal_from_groups(
            subject,
            tenant_id,
            principals,
            registry.get("groups") or {},
            [str(g) for g in groups],
            warehouse_token=user_wh_token,
        )

    # header: trusted SSO / Databricks Apps gateway. The identity header is only
    # believed when the request came through an allowlisted proxy.
    if not trusted_proxies():
        raise AuthError(
            "header auth is not configured (SECURE_QUERY_TRUSTED_PROXIES)", status_code=503
        )
    if not _from_trusted_proxy(client_host):
        raise AuthError("request did not come through a trusted proxy", status_code=403)
    user = (forwarded_user or "").strip()
    if not user:
        raise AuthError("missing identity header", status_code=401)
    return _principal_from_registry(
        user,
        tenant_id,
        principals,
        allow_missing=False,
        warehouse_token=user_wh_token,
    )


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# Asymmetric only: a shared-secret algorithm would let anyone holding the public key forge a token.
_JWT_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"]


@lru_cache(maxsize=4)
def _jwks_client(issuer: str, jwks_url: str):
    import jwt

    if not jwks_url:
        from urllib.request import urlopen

        with urlopen(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10) as response:  # noqa: S310
            jwks_url = json.load(response)["jwks_uri"]
    return jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def _jwt_signing_key(token: str) -> Any:
    """The identity provider's public key for this token (fetched once, cached for an hour)."""
    client = _jwks_client(_env("SECURE_QUERY_OIDC_ISSUER"), _env("SECURE_QUERY_OIDC_JWKS_URL"))
    return client.get_signing_key_from_jwt(token).key


def _jwt_claims(token: str) -> dict[str, Any]:
    """Validate signature, issuer, audience and expiry. Any failure is a 401."""
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover
        raise AuthError('jwt auth needs: pip install "secure-query[enterprise]"', status_code=500) from exc
    try:
        return jwt.decode(
            token,
            _jwt_signing_key(token),
            algorithms=_JWT_ALGORITHMS,
            issuer=_env("SECURE_QUERY_OIDC_ISSUER"),
            audience=_env("SECURE_QUERY_OIDC_AUDIENCE"),
            options={"require": ["exp", "iss", "aud"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {type(exc).__name__}", status_code=401) from exc


def _principal_from_groups(
    subject: str,
    tenant_id: str,
    principals: dict[str, Any],
    group_specs: dict[str, Any],
    groups: list[str],
    *,
    warehouse_token: str | None,
) -> Principal:
    """A principal listed by name wins. Otherwise the caller's groups decide.

    Tables are the union of the matched groups' tables; row filters of every matched group
    all apply, so belonging to more groups never removes a restriction.
    """
    if subject in principals:
        return _principal_from_registry(
            subject, tenant_id, principals, allow_missing=False, warehouse_token=warehouse_token
        )
    matched = [
        _principal_from_registry(g, tenant_id, group_specs, allow_missing=False, warehouse_token=warehouse_token)
        for g in groups
        if g in group_specs
    ]
    if not matched:
        raise AuthError(f"{subject!r} is in no group that has access", status_code=403)
    tables: frozenset[str] | None = frozenset()
    for member in matched:
        tables = None if tables is None or member.allowed_tables is None else tables | member.allowed_tables
    return Principal(
        principal_id=subject,
        tenant_id=matched[0].tenant_id,
        allowed_tables=tables,
        row_filters=tuple(f for member in matched for f in member.row_filters),
        warehouse_token=warehouse_token,
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("missing Authorization header", status_code=401)
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        raise AuthError("Authorization must be Bearer <token>", status_code=401)
    return rest.strip()


def _principal_from_registry(
    principal_id: str,
    tenant_id: str,
    principals: dict[str, Any],
    *,
    allow_missing: bool,
    warehouse_token: str | None = None,
) -> Principal:
    spec = principals.get(principal_id)
    if spec is None:
        if not allow_missing:
            raise AuthError(f"unknown principal {principal_id!r}", status_code=403)
        tables = _env_allowed_tables()
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            allowed_tables=tables,
            warehouse_token=warehouse_token,
        )
    if not isinstance(spec, dict):
        raise AuthError(f"invalid principal spec for {principal_id!r}", status_code=500)
    allowed_raw = spec.get("allowed_tables", [])
    allowed: frozenset[str] | None
    if allowed_raw is None:
        allowed = None
    else:
        allowed = frozenset(str(t) for t in allowed_raw)
    filters: tuple[Filter, ...] = ()
    raw_filters = spec.get("row_filters") or []
    if raw_filters:
        from pydantic import TypeAdapter

        from secure_query.kernel.logical_plan import Filter as FilterUnion

        adapter = TypeAdapter(FilterUnion)
        filters = tuple(adapter.validate_python(f) for f in raw_filters)
    # Request-scoped OBO token wins; registry may store a per-user PAT for token mode.
    reg_token = str(spec.get("databricks_token") or "").strip() or None
    return Principal(
        principal_id=principal_id,
        tenant_id=str(spec.get("tenant_id") or tenant_id),
        allowed_tables=allowed,
        row_filters=filters,
        warehouse_token=warehouse_token or reg_token,
    )


def _env_allowed_tables() -> frozenset[str] | None:
    raw = (os.environ.get("SECURE_QUERY_ALLOWED_TABLES") or "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def load_principal_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Load principals + token map from JSON file or SECURE_QUERY_API_TOKENS."""
    file_path = path or os.environ.get("SECURE_QUERY_PRINCIPALS_FILE")
    registry: dict[str, Any] = {"tenant_id": None, "principals": {}, "tokens": {}}
    if file_path:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise AuthError("principals file must be a JSON object", status_code=500)
        registry.update(data)
    raw_tokens = os.environ.get("SECURE_QUERY_API_TOKENS")
    if raw_tokens:
        extra = json.loads(raw_tokens)
        if not isinstance(extra, dict):
            raise AuthError("SECURE_QUERY_API_TOKENS must be a JSON object", status_code=500)
        tokens = dict(registry.get("tokens") or {})
        principals = dict(registry.get("principals") or {})
        for token, spec in extra.items():
            if isinstance(spec, str):
                tokens[token] = spec
            elif isinstance(spec, dict):
                pid = str(spec.get("principal_id") or token)
                tokens[token] = pid
                principals.setdefault(pid, spec)
            else:
                raise AuthError("invalid token spec", status_code=500)
        registry["tokens"] = tokens
        registry["principals"] = principals
    return registry


def _registry_stamp() -> tuple[str, int, str]:
    """Changes whenever the principals file or the token env changes."""
    path = os.environ.get("SECURE_QUERY_PRINCIPALS_FILE") or ""
    try:
        modified = os.stat(path).st_mtime_ns if path else 0
    except OSError:
        modified = -1
    return path, modified, os.environ.get("SECURE_QUERY_API_TOKENS") or ""


@lru_cache(maxsize=1)
def _registry_for(_stamp: tuple[str, int, str]) -> dict[str, Any]:
    return load_principal_registry()


def principal_registry() -> dict[str, Any]:
    """Process-wide principal registry, re-read when the file or the token env changes."""
    return _registry_for(_registry_stamp())


principal_registry.cache_clear = _registry_for.cache_clear  # type: ignore[attr-defined]
