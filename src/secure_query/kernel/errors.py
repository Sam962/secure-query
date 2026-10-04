"""Typed errors for LQP validation and compilation failures."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ValidationError(BaseModel):
    """A single validation failure with machine-readable code and human-readable message."""

    code: str
    path: str
    message: str
    stage: Literal["structural", "typecheck", "policy", "cost", "compile"]
    severity: Literal["error", "warning"] = "error"

    model_config = ConfigDict(extra="forbid", frozen=True)
