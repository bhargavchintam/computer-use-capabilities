"""The runtime shared by discovery and replay: one live, policy-guarded, recorded session.

`RuntimeSession` owns the browser surface, deterministic sign-on, native-dialog
handling and masked evidence. `RuntimeGuard` watches for known runtime
conditions (from the app profile) and classifies every interruption as one of:

* recoverable      handled here with a bounded handler (and logged as a recovery)
* business_outcome a declared, legitimate result the caller must see
* human_required   needs a person (e.g. supervisor PIN)
* fatal            stop with a debuggable failure
* unrecognized     an unknown dialog or new error text: fail safe, never ignore
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Dialog
from playwright.async_api import Error as PWError

from .checks import Checks, strategy_dicts
from .configio import resolve_secret
from .control import ControlState, SessionController
from .evidence import RunRecorder, utcnow
from .models import AppProfile, Detector, TenantConfig
from .models.capability import DialogExpectation
from .models.conditions import PatternArgs, TextMatches
from .models.results import RecoveryRecord
from .observation import render
from .policy import PolicyEngine
from .redaction import Redactor
from .surface.base import NotReady
from .surface.web import WebSurface

HitKind = Literal["recoverable", "human_required", "business_outcome", "fatal", "unrecognized"]


class AuthFailed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class Hit:
    kind: HitKind
    source: str  # detector id, "dialog", "error_region", or an outcome code
    code: str | None
    message: str
    detector: Detector | None = None
    implies: str | None = None


@dataclass
class DialogEvent:
    type: str
    message: str
    action: str
    kind: Literal["expected", "recoverable", "fatal", "unrecognized", "human"]
    rule: str | None = None
    consumed: bool = False


@dataclass
class RecoveryOutcome:
    status: Literal["handled", "redrive", "exhausted", "not_allowed"]
    detail: str = ""


@dataclass
class OutcomeRule:
    code: str
    condition: Any
    pattern: str | None = None
    container: list[str] = field(default_factory=list)


class RuntimeSession:
    def __init__(
        self,
        *,
        tenant: TenantConfig,
        app: AppProfile,
        policy: PolicyEngine,
        redactor: Redactor,
        recorder: RunRecorder,
        controller: SessionController,
        headed: bool = False,
        record_video: bool = False,
    ) -> None:
        self.tenant = tenant
        self.app = app
        self.policy = policy
        self.redactor = redactor
        self.recorder = recorder
        self.controller = controller
        self.surface = WebSurface(
            tenant.base_url, headed=headed, video_dir=recorder.dir / "video" if record_video else None
        )
        self.dialogs: list[DialogEvent] = []
        self.dialog_expectation: DialogExpectation | None = None
        self.secrets: dict[str, str] = {}
        self.product_version: str | None = None
        self.timeouts = app.timeouts

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for name, ref in self.tenant.secrets.items():
            self.secrets[name] = resolve_secret(ref)
            self.redactor.add_secret(name, self.secrets[name])
        await self.surface.start()
        self.surface.route_guard = self._guard_request
        self.surface.on_dialog = self._on_dialog
        self.surface.on_capture = self.controller.on_capture
        self.controller.on_take_control = self.surface.bring_to_front
        self.recorder.event(
            "session_started",
            tenant=self.tenant.id,
            base_url=self.tenant.base_url,
            environment=self.tenant.environment,
            headed=self.surface.headed,
        )

    async def close(self) -> Path | None:
        video = await self.surface.close()
        self.recorder.event("session_closed", video=str(video.name) if video else None)
        return video

    def _guard_request(self, url: str, resource_type: str) -> bool:
        d = self.policy.url_allowed(url)
        if not d.allowed:
            self.recorder.event(
                "policy_blocked_request", url=url, resource_type=resource_type, reason=d.reason
            )
        return d.allowed

    async def _on_dialog(self, dialog: Dialog) -> None:
        msg, typ = dialog.message, dialog.type
        default = "dismiss" if typ in ("confirm", "prompt", "beforeunload") else "accept"
        kind: Any = "unrecognized"
        action, rule = default, None
        if self.controller.state == ControlState.HUMAN:
            kind, action = "human", "accept"  # the operator triggered it; logged, not second-guessed
        elif self.dialog_expectation and re.search(self.dialog_expectation.match, msg):
            kind, action = "expected", self.dialog_expectation.action
        else:
            for r in self.app.dialogs:
                if re.search(r.match, msg):
                    kind, action, rule = r.kind, r.action, r.description
                    break
        with contextlib.suppress(PWError):
            await (dialog.accept() if action == "accept" else dialog.dismiss())
        ev = DialogEvent(typ, self.redactor.scrub_text(msg), action, kind, rule)
        self.dialogs.append(ev)
        self.recorder.event(
            "dialog", dialog_type=typ, message=ev.message, action=action, classification=kind, rule=rule
        )

    # ------------------------------------------------------------------ navigation & auth
    async def goto(self, route: str) -> None:
        async with self.controller.automated_action():
            await self.surface.goto(route)
        await self.wait_settled()

    async def wait_settled(self, timeout_ms: int = 8000) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while not self.surface.settled(self.timeouts.settle_ms):
            if asyncio.get_running_loop().time() > deadline:
                return
            await asyncio.sleep(0.05)

    async def sign_on(self) -> str | None:
        """Deterministic sign-on from the app profile; credentials never reach the model or logs."""
        auth = self.app.auth
        checks = Checks(self.surface, secrets=self.secrets)
        await self.goto(auth.route)
        for step in auth.steps:
            res = None
            for _ in range(int(self.timeouts.step_ms / self.timeouts.poll_ms)):
                try:
                    res, _, _ = await self.surface.resolve(
                        step.target.container, strategy_dicts(step.target, {})
                    )
                except NotReady:
                    res = None
                if res:
                    break
                await asyncio.sleep(self.timeouts.poll_ms / 1000)
            if res is None:
                raise AuthFailed("form_not_found", f"sign-on control for step {step.id} not found")
            async with self.controller.automated_action():
                if step.action == "fill":
                    assert step.value is not None
                    await self.surface.fill(res.element, checks.resolve_value(step.value))
                else:
                    await self.surface.click(res.element)
        deadline = asyncio.get_running_loop().time() + self.timeouts.step_ms / 1000
        while True:
            try:
                if await checks.holds(auth.success):
                    break
                text = await self.surface.page_text([]) or ""
                for reason, pattern in auth.failures.items():
                    if re.search(pattern, text):
                        raise AuthFailed(reason, f"sign-on failed: {reason}")
            except NotReady:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise AuthFailed("timeout", "sign-on did not reach the post-login state")
            await asyncio.sleep(self.timeouts.poll_ms / 1000)
        await self.wait_settled()
        self.product_version = await self.probe_version()
        self.recorder.event("signed_on", product_version=self.product_version)
        return self.product_version

    async def probe_version(self) -> str | None:
        probe = self.app.version_probe
        try:
            text = await self.surface.page_text(probe.container) or ""
        except NotReady:
            return None
        m = re.search(probe.pattern, text)
        return m.group(1) if m else None

    # ------------------------------------------------------------------ evidence
    async def screenshot(self, label: str, marks: list[tuple[str, list[float]]] | None = None) -> str | None:
        try:
            png = await self.surface.screenshot(
                self.app.sensitive_labels, marks=marks, sensitive_values=self.redactor.identifying_values()
            )
        except Exception:  # noqa: BLE001 - evidence capture must never mask the real error
            return None
        return self.recorder.save_screenshot(png, label)

    async def excerpt(self, max_chars: int = 6000) -> str:
        try:
            snap = await self.surface.snapshot()
        except Exception:  # noqa: BLE001
            return "(snapshot unavailable)"
        return render(snap, self.redactor, for_model=False)[:max_chars]

    async def red_baseline(self) -> dict[tuple[str, ...], set[str]]:
        base: dict[tuple[str, ...], set[str]] = {}
        for container in self.app.error_region.containers:
            try:
                base[tuple(container)] = set(await self.surface.red_texts(container))
            except NotReady:
                base[tuple(container)] = set()
        return base


class RuntimeGuard:
    """Detects runtime conditions (in precedence order) and runs bounded recoveries."""

    def __init__(self, session: RuntimeSession, checks: Checks, outcome_rules: list[OutcomeRule]) -> None:
        self.session = session
        self.checks = checks
        self.outcome_rules = outcome_rules
        self.counts: dict[str, int] = {}
        self.recoveries: list[RecoveryRecord] = []
        self.step_id: str | None = None
        self.recovery_budget = 6

    @staticmethod
    def outcome_rules_from_messages(
        app: AppProfile, codes: dict[str, str] | None = None
    ) -> list[OutcomeRule]:
        """Build outcome rules from the app's message catalog (optionally only the referenced ones)."""
        rules = []
        for msg_id, m in app.messages.items():
            if codes is not None and msg_id not in codes:
                continue
            code = codes[msg_id] if codes is not None else m.outcome
            cond = TextMatches(text_matches=PatternArgs(container=m.container, pattern=m.pattern))
            rules.append(OutcomeRule(code, cond, m.pattern, m.container))
        return rules

    async def check(
        self, *, phase: Literal["pre", "post"], baseline: dict[tuple[str, ...], set[str]] | None = None
    ) -> Hit | None:
        s = self.session
        for ev in s.dialogs:
            if ev.consumed:
                continue
            ev.consumed = True
            if ev.kind == "recoverable":
                self._log_recovery(f"dialog:{ev.rule}", f"{ev.action}ed informational dialog")
            elif ev.kind == "fatal":
                return Hit("fatal", "dialog", "UNRECOGNIZED_STATE", f'{ev.type} dialog: "{ev.message}"')
            elif ev.kind == "unrecognized":
                return Hit(
                    "unrecognized",
                    "dialog",
                    "UNRECOGNIZED_STATE",
                    f'unexpected {ev.type} dialog "{ev.message}" ({ev.action}ed by the safe default)',
                )
        known = await self._known(phase)
        if known is not None:
            return known
        # Fail-safe for anything else the app printed. Only on a settled page, and only after
        # re-checking the known conditions: the text may have rendered after the first pass.
        if phase == "post" and baseline is not None and s.surface.settled(s.timeouts.settle_ms):
            for container in s.app.error_region.containers:
                now = set(await s.surface.red_texts(container))
                new = sorted(now - baseline.get(tuple(container), set()))
                if new:
                    known = await self._known(phase)
                    if known is not None:
                        return known
                    return Hit(
                        "unrecognized",
                        "error_region",
                        "UNRECOGNIZED_STATE",
                        s.redactor.scrub_text("; ".join(new)),
                    )
        return None

    async def _known(self, phase: Literal["pre", "post"]) -> Hit | None:
        s = self.session
        for det in s.app.detectors:
            if await self.checks.holds(det.when):
                message = await self._message_for(det.when) or det.description
                code = det.outcome if det.kind == "business_outcome" else det.failure_code
                return Hit(det.kind, det.id, code, message, det, det.implies)
        if phase == "post":
            for rule in self.outcome_rules:
                if await self.checks.holds(rule.condition):
                    message = (
                        (await self.checks.snippet(rule.pattern, rule.container)) if rule.pattern else None
                    )
                    return Hit(
                        "business_outcome", rule.code, rule.code, s.redactor.scrub_text(message or rule.code)
                    )
        return None

    async def _message_for(self, cond: Any) -> str | None:
        from .models.conditions import condition_kind

        kind = condition_kind(cond)
        if kind == "text_matches":
            snip = await self.checks.snippet(cond.text_matches.pattern, cond.text_matches.container)
            return self.session.redactor.scrub_text(snip) if snip else None
        if kind == "any_of":
            for c in cond.any_of:
                if await self.checks.holds(c):
                    return await self._message_for(c)
        return None

    def _log_recovery(self, detector: str, action: str) -> None:
        rec = RecoveryRecord(detector=detector, step_id=self.step_id, action=action, at=utcnow())
        self.recoveries.append(rec)
        self.session.recorder.event("recovery", detector=detector, step_id=self.step_id, action=action)

    async def recover(self, hit: Hit, *, can_redrive: bool) -> RecoveryOutcome:
        det = hit.detector
        if det is None or det.handler is None:
            return RecoveryOutcome("exhausted", "no handler")
        n = self.counts.get(det.id, 0) + 1
        self.counts[det.id] = n
        self.recovery_budget -= 1
        if n > det.max_times or self.recovery_budget < 0:
            return RecoveryOutcome("exhausted", f"{det.id} occurred {n} times (limit {det.max_times})")
        h = det.handler
        s = self.session
        if h.click is not None:
            res, _, _ = await s.surface.resolve(h.click.container, strategy_dicts(h.click, {}))
            if res is None:
                return RecoveryOutcome("exhausted", "recovery control not found")
            async with s.controller.automated_action():
                await s.surface.click(res.element)
            await asyncio.sleep(s.timeouts.settle_ms / 1000)
            self._log_recovery(det.id, "clicked the acknowledge control")
            return RecoveryOutcome("handled")
        if (h.reauth or h.redrive) and not can_redrive:
            return RecoveryOutcome("not_allowed", "a commit may already have happened; refusing to re-drive")
        if h.reauth:
            await s.sign_on()
            self._log_recovery(det.id, "signed on again")
        if h.redrive:
            self._log_recovery(det.id, "re-driving the flow from the entry route")
            return RecoveryOutcome("redrive")
        return RecoveryOutcome("handled")
