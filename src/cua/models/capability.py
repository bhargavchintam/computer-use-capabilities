"""The capability artifact: a typed, versioned, reviewable, agent-invocable flow.

It has two halves:

* ``contract``: what a caller relies on (typed inputs, typed outputs, declared
  business outcomes with guidance, whether it commits). Overlays can never
  change it. Breaking it is a MAJOR version bump.
* ``implementation``: how the flow is driven (steps, targets, checkpoints,
  outcome detectors, success condition). Version overlays patch it by stable
  step id; locator/detector tweaks are PATCH bumps, flow changes MINOR.

Approval signs a content hash that excludes ``status`` and ``approval`` itself,
so editing an approved file silently turns it back into a draft.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import (
    CAPABILITY_ID,
    OUTCOME_CODE,
    SEMVER,
    SLUG,
    ParamRef,
    ScalarType,
    Sensitivity,
    Strict,
    ValueRef,
)
from .conditions import Condition, condition_kind
from .targets import Target

Action = Literal["click", "fill", "select", "press_key", "extract", "extract_table"]
Effect = Literal["read", "input", "commit"]
InputType = Literal["string", "integer", "money", "date", "enum"]
OutputType = Literal["string", "integer", "money", "date", "identifier", "table"]


# ---------------------------------------------------------------------------- contract


class InputSpec(Strict):
    type: InputType
    description: str
    pattern: str | None = None
    enum: list[str] | None = None
    max_value: str | None = None  # decimal string bound for money/integer
    sensitivity: Sensitivity = "internal"
    required: bool = True


class OutputSpec(Strict):
    type: OutputType
    description: str
    currency: str | None = None
    columns: dict[str, ScalarType] | None = None  # for type=table
    sensitivity: Sensitivity = "internal"

    @model_validator(mode="after")
    def _table_needs_columns(self) -> OutputSpec:
        if (self.type == "table") != (self.columns is not None):
            raise ValueError("columns is required for (and only for) table outputs")
        return self


class OutcomeSpec(Strict):
    """A legitimate business result the caller must handle (not an error)."""

    description: str
    caller_guidance: str
    retry_safe: bool = True


class CallExample(Strict):
    input: dict[str, str]
    output: dict[str, Any]


class Contract(Strict):
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    effects: Literal["read_only", "commit"]
    idempotent: bool
    example: CallExample | None = None

    @model_validator(mode="after")
    def _names(self) -> Contract:
        import re

        for name in [*self.inputs, *self.outputs]:
            if not re.match(SLUG, name):
                raise ValueError(f"input/output names must be snake_case slugs: {name!r}")
        for code in self.outcomes:
            if not re.match(OUTCOME_CODE, code):
                raise ValueError(f"outcome codes must be UPPER_SNAKE: {code!r}")
        if self.idempotent != (self.effects == "read_only"):
            raise ValueError("idempotent must be true exactly when effects == read_only")
        return self


# ---------------------------------------------------------------------------- implementation


class DialogExpectation(Strict):
    """A native dialog the flow expects at this step, and how to answer it."""

    match: str
    action: Literal["accept", "dismiss"]


class Step(Strict):
    id: str = Field(pattern=SLUG)
    intent: str
    action: Action
    effect: Effect
    target: Target
    value: ValueRef | None = None  # fill / select
    key: Literal["Enter", "Tab", "Escape"] | None = None  # press_key
    output: str | None = None  # extract / extract_table
    parse: ScalarType | None = None  # extract
    columns: dict[str, str] | None = None  # extract_table: output column -> header text
    pre: list[Condition] = Field(default_factory=list)
    expect: list[Condition] = Field(default_factory=list)
    on_dialog: DialogExpectation | None = None
    optional: bool = False
    timeout_ms: int = Field(default=10_000, ge=500, le=120_000)
    provenance: Literal["agent", "human", "author"] = "agent"

    @model_validator(mode="after")
    def _shape(self) -> Step:
        a = self.action
        if a in ("fill", "select") and self.value is None:
            raise ValueError(f"step {self.id}: {a} needs a value")
        if a == "press_key" and self.key is None:
            raise ValueError(f"step {self.id}: press_key needs a key")
        if a == "extract" and (self.output is None or self.parse is None):
            raise ValueError(f"step {self.id}: extract needs output and parse")
        if a == "extract_table" and (self.output is None or not self.columns):
            raise ValueError(f"step {self.id}: extract_table needs output and columns")
        if self.effect == "commit" and a not in ("click", "press_key"):
            raise ValueError(f"step {self.id}: only click/press_key can commit")
        if a in ("extract", "extract_table") and self.effect != "read":
            raise ValueError(f"step {self.id}: extraction is always effect=read")
        return self


class AppBinding(Strict):
    product: str = Field(pattern=SLUG)
    versions: str  # PEP 440 specifier set, e.g. ">=7.2,<8.0"
    surface: Literal["web", "desktop", "pixel"] = "web"


class Entry(Strict):
    route: str  # tenant-relative


class OutcomeDetector(Strict):
    """How a declared outcome shows up on screen: a reference to the app profile's
    message catalog (shared across capabilities) or an inline condition."""

    ref: str | None = None
    when: Condition | None = None

    @model_validator(mode="after")
    def _one(self) -> OutcomeDetector:
        if (self.ref is None) == (self.when is None):
            raise ValueError("outcome detector needs exactly one of ref / when")
        return self


class Implementation(Strict):
    app: AppBinding
    entry: Entry
    preconditions: list[Literal["authenticated_session"]] = Field(
        default_factory=lambda: ["authenticated_session"]
    )
    steps: list[Step] = Field(min_length=1)
    outcome_detectors: dict[str, OutcomeDetector] = Field(default_factory=dict)
    success: Condition


# ---------------------------------------------------------------------------- envelope


class Provenance(Strict):
    source: Literal["discovery", "author"]
    recorded_at: str
    goal: str | None = None
    run_id: str | None = None
    model: str | None = None
    tenant: str | None = None
    environment: Literal["sandbox", "production"] | None = None
    trace_sha256: str | None = None
    verified_by_runs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Approval(Strict):
    approved_by: str
    approved_at: str
    content_sha256: str


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


class Capability(Strict):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=CAPABILITY_ID)
    version: str = Field(pattern=SEMVER)
    status: Literal["draft", "approved", "deprecated"] = "draft"
    title: str
    description: str
    contract: Contract
    implementation: Implementation
    provenance: Provenance
    approval: Approval | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def content_sha256(self) -> str:
        return sha256_of(self.model_dump(mode="json", exclude_none=True, exclude={"status", "approval"}))

    def approval_valid(self) -> bool:
        return (
            self.status == "approved"
            and self.approval is not None
            and self.approval.content_sha256 == self.content_sha256()
        )

    def step(self, step_id: str) -> Step:
        return next(s for s in self.implementation.steps if s.id == step_id)

    @model_validator(mode="after")
    def _consistent(self) -> Capability:
        steps = self.implementation.steps
        ids = [s.id for s in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        inputs = self.contract.inputs
        for s in steps:
            for ref in _param_refs(s):
                if ref not in inputs:
                    raise ValueError(f"step {s.id} references unknown input {ref!r}")
        extracted = {s.output for s in steps if s.output}
        declared = set(self.contract.outputs)
        if extracted != declared:
            raise ValueError(
                f"extract steps {sorted(extracted)} must match contract outputs {sorted(declared)}"
            )
        for code in self.implementation.outcome_detectors:
            if code not in self.contract.outcomes:
                raise ValueError(f"outcome detector for undeclared outcome {code!r}")
        commits = any(s.effect == "commit" for s in steps)
        if commits != (self.contract.effects == "commit"):
            raise ValueError("contract.effects must be 'commit' exactly when a step commits")
        condition_kind(self.implementation.success)
        return self


def _param_refs(step: Step) -> set[str]:
    refs: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, ParamRef):
            refs.add(node.param)
        elif isinstance(node, Strict):
            for name in type(node).model_fields:
                walk(getattr(node, name))
        elif isinstance(node, list | tuple):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for x in node.values():
                walk(x)

    walk(step)
    return refs


# Resolve forward references used by ValueRef/Condition unions.
Step.model_rebuild()
Capability.model_rebuild()
_ = ValueRef
