"""How a recorded step identifies the control it acts on.

Strategies are *semantic first* and surface-neutral: each one maps to a concept
a desktop accessibility API also has (UIA ControlType+Name, LabeledBy,
Grid/Table patterns, AutomationId). They are ordered, and every strategy in a
saved artifact was proven at record time to resolve to exactly the element the
agent (or human) acted on. Structural paths and visible text live only in the
fingerprint, for diagnostics; they are never used to act.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import Container, ParamRef, Strict


class RoleName(Strict):
    """Role plus accessible name, e.g. link "Member Inquiry"."""

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str


class LabelStrategy(Strict):
    """A control found by its label: explicit <label>/aria, or the text a human reads
    beside it (legacy layout tables put "Member #:" in the previous cell)."""

    kind: Literal["label"] = "label"
    role: str
    label: str


class RowKey(Strict):
    column: str
    equals: str | ParamRef


class TableCell(Strict):
    """A value cell addressed like a human reads a grid: which table (by its header
    set), which row (by a key column), which column (by header). Never by the
    cell's own text, which is data and changes per invocation."""

    kind: Literal["table_cell"] = "table_cell"
    headers: list[str] = Field(min_length=1)
    row: RowKey
    column: str


class TableStrategy(Strict):
    """A whole data table, identified by (a subset of) its header row."""

    kind: Literal["table"] = "table"
    headers: list[str] = Field(min_length=1)


class AttrStrategy(Strict):
    """Stable markup attributes (name, type, alt, href path...). Auto-generated
    looking ids are rejected at record time."""

    kind: Literal["attr"] = "attr"
    tag: str
    attrs: dict[str, str] = Field(min_length=1)


Strategy = Annotated[
    RoleName | LabelStrategy | TableCell | TableStrategy | AttrStrategy,
    Field(discriminator="kind"),
]


class Fingerprint(Strict):
    """What the element looked like at record time. Diagnostics and drift signals only."""

    tag: str
    role: str | None = None
    name: str | None = None
    label: str | None = None
    css_path: str | None = None
    bbox: list[float] | None = None


class Target(Strict):
    container: Container = Field(default_factory=list)
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint | None = None
