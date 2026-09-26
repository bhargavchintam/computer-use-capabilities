"""The goal spec: the typed intent a discovery run must satisfy.

A natural-language goal ("look up member 12345 and read their savings
balance") is turned into this by one structured-output call, and a human can
review or edit it. The contract of the resulting capability comes from here;
the LLM only discovers *how*.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .capability import InputSpec, InputType, OutputSpec, OutputType
from .common import ScalarType, Sensitivity, Strict

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


class GoalSpec(Strict):
    capability_id: str  # without product prefix, e.g. "member.get_share_balance"
    title: str
    goal: str  # template with {{placeholders}}
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    sample_inputs: dict[str, str]

    @model_validator(mode="after")
    def _placeholders(self) -> GoalSpec:
        used = set(PLACEHOLDER.findall(self.goal))
        missing = used - set(self.inputs)
        if missing:
            raise ValueError(f"goal uses undeclared placeholders: {sorted(missing)}")
        absent = set(self.inputs) - set(self.sample_inputs)
        if absent:
            raise ValueError(f"sample_inputs missing values for: {sorted(absent)}")
        return self


# Shape used for the structured-output LLM call (lists are easier for the model
# than open-ended dicts). Converted into GoalSpec after validation.


class DraftInput(BaseModel):
    name: str = Field(description="snake_case parameter name, e.g. member_number")
    type: InputType
    description: str
    pattern: str | None = Field(default=None, description="conservative regex for identifiers, or null")
    enum: list[str] | None = None
    sensitivity: Sensitivity
    sample_value: str = Field(description="the concrete value taken from the goal text")


class DraftColumn(BaseModel):
    name: str
    type: ScalarType


class DraftOutput(BaseModel):
    name: str = Field(description="snake_case output name")
    type: OutputType
    description: str
    sensitivity: Sensitivity
    currency: str | None = None
    columns: list[DraftColumn] | None = None


class GoalSpecDraft(BaseModel):
    capability_id: str = Field(
        description="dotted snake_case, e.g. member.get_share_balance (no product prefix)"
    )
    title: str
    goal_template: str = Field(description="the goal with every concrete input replaced by {{name}}")
    inputs: list[DraftInput]
    outputs: list[DraftOutput]
    effect_hint: Literal["read_only", "commit"]

    def to_spec(self) -> GoalSpec:
        return GoalSpec(
            capability_id=self.capability_id,
            title=self.title,
            goal=self.goal_template,
            inputs={
                i.name: InputSpec(
                    type=i.type,
                    description=i.description,
                    pattern=i.pattern,
                    enum=i.enum,
                    sensitivity=i.sensitivity,
                )
                for i in self.inputs
            },
            outputs={
                o.name: OutputSpec(
                    type=o.type,
                    description=o.description,
                    currency=o.currency or ("USD" if o.type == "money" else None),
                    columns={c.name: c.type for c in o.columns} if o.columns else None,
                    sensitivity=o.sensitivity,
                )
                for o in self.outputs
            },
            sample_inputs={i.name: i.sample_value for i in self.inputs},
        )
