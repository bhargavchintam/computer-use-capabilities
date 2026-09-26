"""Allowlist enforcement and risk classification.

Everything the automation does passes through here: pre-flight checks on a
capability, every request the browser makes (network guard), every action
type, and the effect class of every control it touches. Human operators are
not blocked by policy (they are accountable and audited), but their actions
are recorded.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from .models import AppProfile, Capability, Effect, Policy, TenantConfig

_EFFECT_RANK = {"read": 0, "input": 1, "commit": 2}
CommitGate = Literal["allow", "needs_approval", "deny"]


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(f"^{out}$")


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class PolicyEngine:
    def __init__(self, policy: Policy, tenant: TenantConfig, app: AppProfile) -> None:
        self.policy = policy
        self.tenant = tenant
        self.origins = {_origin(tenant.base_url), *(_origin(o) for o in policy.extra_origins)}
        self._allow = [_glob_to_regex(g) for g in policy.allow_paths]
        self._deny = [_glob_to_regex(g) for g in policy.deny_paths]
        self._commit_name = re.compile(app.risk.commit_names)
        self._commit_routes = app.risk.commit_routes

    # ------------------------------------------------------------------ routes / network
    def path_allowed(self, path: str) -> Decision:
        if any(r.match(path) for r in self._deny):
            return Decision(False, f"path {path} is explicitly denied")
        if not any(r.match(path) for r in self._allow):
            return Decision(False, f"path {path} is not on the allowlist")
        return Decision(True, "allowed")

    def url_allowed(self, url: str) -> Decision:
        u = urlparse(url)
        if u.scheme in ("about", "data", "blob"):
            return Decision(True, "local scheme")
        if _origin(url) not in self.origins:
            return Decision(False, f"origin {_origin(url)} is not allowed for tenant {self.tenant.id}")
        return self.path_allowed(u.path or "/")

    def request_allowed(self, url: str, resource_type: str) -> bool:
        return self.url_allowed(url).allowed

    # ------------------------------------------------------------------ actions
    def action_allowed(self, action: str) -> Decision:
        if action not in self.policy.allowed_actions:
            return Decision(False, f"action {action!r} is not an allowed action type")
        return Decision(True, "allowed")

    def classify(self, action: str, control: dict[str, Any] | None, recorded: Effect | None = None) -> Effect:
        """Effect class of an action on a concrete control. Can raise, never lower, a recorded class."""
        if action in ("extract", "extract_table"):
            effect: Effect = "read"
        elif action in ("fill", "select"):
            effect = "input"
        else:
            effect = "read"
            if control:
                name = (control.get("name") or "").strip()
                if name and self._commit_name.search(name):
                    effect = "commit"
                action_path = control.get("form_action") or ""
                if control.get("submits") and any(
                    fnmatch.fnmatch(action_path, p) for p in self._commit_routes
                ):
                    effect = "commit"
                if (
                    action == "press_key"
                    and control.get("form_action")
                    and any(fnmatch.fnmatch(action_path, p) for p in self._commit_routes)
                ):
                    effect = "commit"
        if recorded and _EFFECT_RANK[recorded] > _EFFECT_RANK[effect]:
            return recorded
        return effect

    def commit_gate(self, *, invocation_approved: bool) -> CommitGate:
        if not self.policy.commit.require_approval:
            return "allow"
        return "allow" if invocation_approved else "needs_approval"

    # ------------------------------------------------------------------ pre-flight
    def preflight(self, cap: Capability) -> list[tuple[str, str]]:
        """Violations as (failure_code, message); empty means the capability may run here."""
        problems: list[tuple[str, str]] = []
        for step in cap.implementation.steps:
            d = self.action_allowed(step.action)
            if not d.allowed:
                problems.append(("POLICY_DENIED", f"step {step.id}: {d.reason}"))
        d = self.path_allowed(cap.implementation.entry.route)
        if not d.allowed:
            problems.append(("POLICY_DENIED", f"entry route: {d.reason}"))
        commit = self.policy.commit
        if self.tenant.environment == "production" and commit.production_requires_approved_capability:
            if not cap.approval_valid():
                why = "edited after approval" if cap.status == "approved" else f"status is {cap.status}"
                problems.append(("NOT_APPROVED", f"{cap.ref} is not approved for production ({why})"))
        elif cap.status == "deprecated":
            problems.append(("NOT_APPROVED", f"{cap.ref} is deprecated"))
        elif cap.status == "draft" and not commit.allow_draft_in_sandbox:
            problems.append(("NOT_APPROVED", f"{cap.ref} is a draft and drafts are not allowed here"))
        return problems
