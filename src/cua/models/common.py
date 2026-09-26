"""Shared building blocks for every YAML-backed model.

All artifact/config models are *strict*: YAML must spell types exactly (a
member number is a quoted string, never an int), unknown keys are rejected,
and values that feed hashes are plain JSON types (money is a decimal string).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

SLUG = r"^[a-z][a-z0-9_]*$"
CAPABILITY_ID = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
SEMVER = r"^\d+\.\d+\.\d+$"
OUTCOME_CODE = r"^[A-Z][A-Z0-9_]*$"

Sensitivity = Literal["public", "internal", "pii_identifier", "pii", "confidential"]
ScalarType = Literal["string", "integer", "money", "date", "identifier"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ParamRef(Strict):
    """A value supplied by the caller at invocation time."""

    param: str = Field(pattern=SLUG)


class SecretRef(Strict):
    """A credential resolved from the tenant's secret store at runtime; never stored."""

    secret: str = Field(pattern=SLUG)


class LiteralValue(Strict):
    """A fixed UI value (e.g. a menu option). Linted so it can't smuggle data."""

    literal: str


def _value_tag(v: Any) -> str | None:
    if isinstance(v, dict):
        return next(iter(v)) if len(v) == 1 else None
    for name in ("param", "secret", "literal"):
        if hasattr(v, name):
            return name
    return None


ValueRef = Annotated[
    Annotated[ParamRef, Tag("param")]
    | Annotated[SecretRef, Tag("secret")]
    | Annotated[LiteralValue, Tag("literal")],
    Discriminator(_value_tag),
]

Container = list[str]
"""Path to the region that holds a control, outermost first.

Web: ``["frame:work"]`` (frame names; ``[]`` is the top document).
Desktop (design): ``["window:Core Banking", "pane:Member Search"]``.
"""
