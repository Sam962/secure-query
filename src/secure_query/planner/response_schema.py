"""Strict structured-output schema for the LogicalPlan planner (OpenAI json_schema).

The response shape comes from the Pydantic models, not prompt prose: the API
enforces required fields and types, so malformed JSON and missing fields stop
costing repair calls. Strict mode needs a single root object, so plans,
approved-metric choices and refusals share one wrapper, unwrapped by
`unwrap_response` into the shapes parse_plan_json already understands.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from secure_query.kernel.logical_plan import LogicalPlan

# Keywords strict mode rejects or that only restate what Pydantic validates.
_DROP = frozenset(
    {
        "default", "title", "description", "discriminator", "format", "examples",
        "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "minLength", "maxLength", "pattern",
    }
)
# Assigned or rebuilt server-side; the model must not write them.
_SERVER_FIELDS = ("plan_id", "schema_version", "joins")
_KINDS = ("plan", "metric", "refusal")


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strict(v) for k, v in node.items() if k not in _DROP}
    if "oneOf" in out:
        out["anyOf"] = out.pop("oneOf")
    if "const" in out:
        out["enum"] = [out.pop("const")]
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


@lru_cache(maxsize=1)
def plan_response_format() -> dict[str, Any]:
    """`response_format` for chat.completions: one plan, metric choice, or refusal."""
    schema = LogicalPlan.model_json_schema()
    defs = schema.pop("$defs")
    for field in _SERVER_FIELDS:
        schema["properties"].pop(field, None)
    # Strict mode cannot take a str | int | float | bool union: send a string,
    # LiteralValue coerces it by its `type`.
    defs["LiteralValue"]["properties"]["value"] = {"type": "string"}
    defs["LogicalPlan"] = schema
    root = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(_KINDS)},
            "plan": {"anyOf": [{"$ref": "#/$defs/LogicalPlan"}, {"type": "null"}]},
            "metric_id": {"type": ["string", "null"]},
            "limit": {"type": ["integer", "null"]},
            "reason": {"type": ["string", "null"]},
        },
        "$defs": defs,
    }
    return {
        "type": "json_schema",
        "json_schema": {"name": "plan_response", "strict": True, "schema": _strict(root)},
    }


def unwrap_response(data: dict[str, Any]) -> dict[str, Any]:
    """Wrapper → the legacy shapes: plan dict, {"metric_id", "limit"}, or a refusal."""
    if data.get("kind") not in _KINDS or "plan" not in data:
        return data
    if data["kind"] == "refusal":
        return {"cannot_answer": True, "reason": data.get("reason") or ""}
    if data["kind"] == "metric":
        return {"metric_id": data.get("metric_id"), "limit": data.get("limit")}
    return dict(data.get("plan") or {})
