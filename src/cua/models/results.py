"""The result contract every run returns to its caller.

Status tells the caller what kind of answer it got:

* ``succeeded``         outputs are present and typed; success was verified.
* ``business_outcome``  a declared, legitimate result (e.g. MEMBER_NOT_FOUND).
* ``rejected``          refused before touching the UI (bad input, policy, approval, version).
* ``needs_human``       parked on an intervention request; see ``parked``.
* ``failed``            a hard failure with enough detail to debug.

``side_effect`` answers the first question after any failure on a write flow:
did anything commit? ``retry_safe`` is derived from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["succeeded", "business_outcome", "rejected", "needs_human", "failed"]
SideEffect = Literal["none", "not_committed", "committed", "unknown"]
FailureCode = Literal[
    # pre-flight (nothing touched)
    "INPUT_INVALID",
    "POLICY_DENIED",
    "NOT_APPROVED",
    "INCOMPATIBLE_VERSION",
    "CAPABILITY_INVALID",
    # runtime
    "TARGET_NOT_FOUND",
    "AMBIGUOUS_TARGET",
    "TIMEOUT",
    "CHECKPOINT_FAILED",
    "UNRECOGNIZED_STATE",
    "APP_ERROR",
    "PERMISSION_DENIED",
    "AUTH_FAILED",
    "SESSION_LOST",
    "RESYNC_FAILED",
    "ABORTED_BY_OPERATOR",
    "DISCOVERY_STUCK",
    "INTERNAL",
]
RETRYABLE: frozenset[str] = frozenset({"TIMEOUT", "APP_ERROR", "SESSION_LOST"})


class OutcomeInfo(BaseModel):
    code: str
    description: str
    caller_guidance: str
    retry_safe: bool
    message: str | None = None  # what the app displayed (redacted)
    step_id: str | None = None


class FailureInfo(BaseModel):
    code: FailureCode
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    reason: str | None = None  # e.g. AUTH_FAILED reason
    hint: str | None = None
    near_misses: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class RecoveryRecord(BaseModel):
    detector: str
    step_id: str | None
    action: str
    at: str


class WarningRecord(BaseModel):
    code: str
    step_id: str | None = None
    detail: str


class HumanAction(BaseModel):
    type: str
    container: list[str] = Field(default_factory=list)
    target: str
    value: str | None = None  # redacted; never captured for secret fields
    at: str


class InterventionRecord(BaseModel):
    id: str
    kind: str
    reason: str
    step_id: str | None = None
    decision: str | None = None
    operator: str | None = None
    note: str | None = None
    requested_at: str
    resolved_at: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)


class Parked(BaseModel):
    intervention_id: str
    deadline: str
    session_retained: bool
    request_path: str


class StepReport(BaseModel):
    step_id: str
    status: Literal["done", "skipped", "completed_by_human", "outcome", "awaiting_human", "failed", "not_run"]
    strategy: str | None = None
    attempts: int = 0
    duration_ms: int = 0


class RunResult(BaseModel):
    run_id: str
    mode: Literal["replay", "discovery"]
    capability: str | None = None
    tenant: str
    effective_plan_sha256: str | None = None
    overlays_applied: list[str] = Field(default_factory=list)
    status: RunStatus
    outputs: dict[str, Any] | None = None
    outcome: OutcomeInfo | None = None
    error: FailureInfo | None = None
    side_effect: SideEffect = "none"
    retry_safe: bool = True
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    parked: Parked | None = None
    steps: list[StepReport] = Field(default_factory=list)
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    evidence_dir: str | None = None
