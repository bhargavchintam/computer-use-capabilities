"""Configuration layers, from most shared to most specific.

* ``AppProfile``: vendor-product knowledge shared by every tenant running it
  (auth procedure, runtime detectors, message catalog, sensitive labels, risk
  rules, timeouts). Written once per product by an engineer.
* ``Overlay``: vendor-version patches (re-target, never re-route) shared by all
  tenants on that release.
* ``TenantConfig``: one institution: base URL, product version, environment,
  secret references, policy, overlays.
* ``Policy``: the allowlist and commit rules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .capability import Action, OutcomeDetector, Step
from .common import Container, Strict
from .conditions import Condition
from .targets import Target

DetectorKind = Literal["recoverable", "human_required", "business_outcome", "fatal"]
SideEffectHint = Literal["not_committed", "committed", "unknown"]


class Handler(Strict):
    """What the runtime does for a recoverable condition. Deliberately limited:
    click a known control, re-authenticate, or re-drive from the entry route
    (only allowed while nothing has been committed). Never "reload"."""

    click: Target | None = None
    reauth: bool = False
    redrive: bool = False


class Detector(Strict):
    id: str
    description: str
    when: Condition
    kind: DetectorKind
    handler: Handler | None = None
    max_times: int = 2
    failure_code: str | None = None  # fatal, or recoverable after max_times
    outcome: str | None = None  # business_outcome
    implies: SideEffectHint | None = None  # what this state means for a pending commit


class DialogRule(Strict):
    match: str  # regex over the dialog message
    action: Literal["accept", "dismiss"]
    kind: Literal["recoverable", "fatal"]
    description: str


class MessageRule(Strict):
    """A product-wide on-screen message and the business outcome it means."""

    pattern: str
    container: Container = Field(default_factory=list)
    outcome: str
    description: str
    caller_guidance: str
    retry_safe: bool = True


class AuthProcedure(Strict):
    route: str
    steps: list[Step]
    success: Condition
    failures: dict[str, str]  # AUTH_FAILED reason -> regex


class VersionProbe(Strict):
    container: Container
    pattern: str  # one capture group with the product version


class ErrorRegion(Strict):
    """Where this product prints messages; unknown new text here fails safe."""

    containers: list[Container]
    kind: Literal["red_text"] = "red_text"


class RiskRules(Strict):
    commit_names: str  # regex over control accessible names
    commit_routes: list[str]  # fnmatch patterns over form action paths


class Timeouts(Strict):
    step_ms: int = 10_000
    poll_ms: int = 250
    settle_ms: int = 350
    handoff_ms: int = 900_000


class AppProfile(Strict):
    product: str
    profile_version: str
    description: str
    versions_supported: str
    version_probe: VersionProbe
    auth: AuthProcedure
    detectors: list[Detector]
    dialogs: list[DialogRule]
    messages: dict[str, MessageRule]
    error_region: ErrorRegion
    sensitive_labels: list[str]
    risk: RiskRules
    timeouts: Timeouts = Field(default_factory=Timeouts)


class CommitPolicy(Strict):
    require_approval: bool = True
    production_requires_approved_capability: bool = True
    allow_draft_in_sandbox: bool = True


class Policy(Strict):
    id: str
    allowed_actions: list[Action]
    allow_paths: list[str]
    deny_paths: list[str]
    extra_origins: list[str] = Field(default_factory=list)
    commit: CommitPolicy = Field(default_factory=CommitPolicy)


class TenantConfig(Strict):
    id: str
    display_name: str
    environment: Literal["sandbox", "production"]
    app: str
    product_version: str
    base_url: str
    secrets: dict[str, str]  # name -> "env:VAR"
    policy: str
    overlays: list[str] = Field(default_factory=list)


class StepPatch(Strict):
    """Re-target a step, never re-route: a patch may swap how a control is found,
    what the landing page looks like, or how long to wait, but it cannot add,
    remove or reorder steps (that is a new capability version)."""

    target: Target | None = None
    expect: list[Condition] | None = None
    timeout_ms: int | None = None


class CapabilityPatch(Strict):
    steps: dict[str, StepPatch] = Field(default_factory=dict)
    outcome_detectors: dict[str, OutcomeDetector] = Field(default_factory=dict)


class AppliesTo(Strict):
    product: str
    versions: str


class Overlay(Strict):
    id: str
    description: str
    applies_to: AppliesTo
    capabilities: dict[str, CapabilityPatch] = Field(default_factory=dict)
    labels: dict[str, dict[str, str]] = Field(default_factory=dict)  # input -> {value: UI label}
