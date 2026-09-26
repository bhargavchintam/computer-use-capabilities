"""Deterministic replay: the production execution path. No model is imported here.

Every wait is a race between the state we expect, every known runtime
condition, and a timeout, so nothing ever proceeds blindly and nothing sleeps
blindly. Every exit is classified into the result contract (see models/results.py).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, get_args

from .checks import Checks, strategy_dicts
from .configio import load_app_profile, load_overlays, load_policy, load_tenant, repo_root
from .control import ControlLost, ControlState, DecisionKind, InterventionKind, SessionController
from .control import Resolution as Handoff
from .evidence import RunRecorder, utcnow
from .models import Capability, Step, describe
from .models.conditions import PatternArgs, TextMatches
from .models.results import (
    RETRYABLE,
    FailureCode,
    FailureInfo,
    InterventionRecord,
    OutcomeInfo,
    Parked,
    RunResult,
    StepReport,
    WarningRecord,
)
from .policy import PolicyEngine
from .redaction import Redactor
from .runtime import AuthFailed, Hit, OutcomeRule, RuntimeGuard, RuntimeSession
from .surface.base import NotReady, SessionLost
from .surface.web import Resolution
from .tenancy import EffectivePlan, resolve_plan, version_in

OperatorMode = Literal["none", "console", "scripted"]
OperatorHook = Callable[[SessionController, RuntimeSession], Coroutine[Any, Any, None]]
MAX_REDRIVES = 2


# ---------------------------------------------------------------------------------- helpers


class _Stop(Exception):  # noqa: N818 - control flow, not an error
    def __init__(
        self,
        status: str,
        *,
        outcome: OutcomeInfo | None = None,
        error: FailureInfo | None = None,
        parked: Parked | None = None,
    ) -> None:
        super().__init__(status)
        self.status, self.outcome, self.error, self.parked = status, outcome, error, parked


class _Redrive(Exception):  # noqa: N818
    pass


def validate_inputs(cap: Capability, raw: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Check caller inputs against the contract. Messages never echo the value."""
    errors: list[str] = []
    clean: dict[str, str] = {}
    for name in sorted(set(raw) - set(cap.contract.inputs)):
        errors.append(f"{name}: not an input of {cap.id}")
    for name, spec in cap.contract.inputs.items():
        value = (raw.get(name) or "").strip()
        if not value:
            if spec.required:
                errors.append(f"{name}: required")
            continue
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            errors.append(f"{name}: does not match the required format {spec.pattern}")
        if spec.type == "enum" and spec.enum and value not in spec.enum:
            errors.append(f"{name}: must be one of {spec.enum}")
        if spec.type == "money":
            try:
                amount = Decimal(value.replace(",", "").replace("$", ""))
                if amount <= 0:
                    errors.append(f"{name}: must be positive")
                if spec.max_value and amount > Decimal(spec.max_value):
                    errors.append(f"{name}: exceeds the maximum {spec.max_value}")
            except InvalidOperation:
                errors.append(f"{name}: not a valid amount")
        if spec.type == "integer" and not re.fullmatch(r"-?\d+", value):
            errors.append(f"{name}: not an integer")
        clean[name] = value
    return clean, errors


def failure_code(value: str | None, default: FailureCode) -> FailureCode:
    """Codes in app-profile config are strings; only known failure codes reach the result contract."""
    return value if value in get_args(FailureCode) else default  # type: ignore[return-value]


def parse_value(text: str, kind: str, currency: str | None = None) -> Any:
    t = (text or "").strip()
    if kind == "money":
        m = re.search(r"-?\$?\s?-?[\d,]*\d(?:\.\d+)?", t)
        if not m:
            raise ValueError("no amount found")
        amount = Decimal(re.sub(r"[^\d.\-]", "", m.group()))
        return {"amount": f"{amount:.2f}", "currency": currency or "USD"}
    if kind == "integer":
        return int(re.sub(r"[,\s]", "", t))
    if not t:
        raise ValueError("empty value")
    return t


def _expect_kinds(step: Step) -> set[str]:
    from .models.conditions import condition_kind

    return {condition_kind(c) for c in step.expect}


# ---------------------------------------------------------------------------------- engine


class ReplayEngine:
    def __init__(
        self,
        plan: EffectivePlan,
        *,
        session: RuntimeSession,
        controller: SessionController,
        recorder: RunRecorder,
        redactor: Redactor,
        policy: PolicyEngine,
        inputs: dict[str, str],
        tenant_version: str,
        invocation_approved: bool,
    ) -> None:
        self.plan = plan
        self.cap = plan.capability
        self.session = session
        self.surface = session.surface
        self.controller = controller
        self.recorder = recorder
        self.redactor = redactor
        self.policy = policy
        self.tenant_version = tenant_version
        self.invocation_approved = invocation_approved
        self.checks = Checks(
            self.surface,
            params=inputs,
            input_specs=self.cap.contract.inputs,
            labels=plan.labels,
            secrets=session.secrets,
        )
        self.guard = RuntimeGuard(session, self.checks, self._outcome_rules())
        self.timeouts = session.app.timeouts
        self.reports: dict[str, StepReport] = {
            s.id: StepReport(step_id=s.id, status="not_run") for s in self.cap.implementation.steps
        }
        self.warnings: list[WarningRecord] = []
        self.interventions: list[InterventionRecord] = []
        self.approved_steps: set[str] = set()
        self.commit_dispatched: str | None = None
        self.committed = False
        self.side_effect_hint: str | None = None
        self.redrives = 0
        self._diag: tuple[list[tuple[dict[str, Any], int]], bool] = ([], False)

    def _outcome_rules(self) -> list[OutcomeRule]:
        app = self.session.app
        rules: list[OutcomeRule] = []
        for code, det in self.cap.implementation.outcome_detectors.items():
            if det.ref is not None:
                m = app.messages[det.ref]
                rules.append(
                    OutcomeRule(
                        code,
                        TextMatches(text_matches=PatternArgs(container=m.container, pattern=m.pattern)),
                        m.pattern,
                        m.container,
                    )
                )
            else:
                rules.append(OutcomeRule(code, det.when))
        return rules

    # ------------------------------------------------------------------ top level
    async def run(self) -> tuple[str, OutcomeInfo | None, FailureInfo | None, Parked | None]:
        steps = self.cap.implementation.steps
        try:
            version = await self.session.sign_on()
            if version and not version_in(self.cap.implementation.app.versions, version):
                raise await self._fail(
                    "INCOMPATIBLE_VERSION",
                    f"the app reports AcmeCore {version}, outside {self.cap.implementation.app.versions}",
                    status="rejected",
                )
            if version and version != self.tenant_version:
                self._warn(
                    "PRODUCT_VERSION_MISMATCH",
                    None,
                    f"tenant config says {self.tenant_version} but the app reports {version}",
                )
            await self.session.goto(self.cap.implementation.entry.route)
            i = 0
            while i < len(steps):
                try:
                    await self._run_step(steps[i])
                    i += 1
                except _Redrive:
                    self.redrives += 1
                    if self.redrives > MAX_REDRIVES:
                        raise await self._fail(
                            "APP_ERROR", "the flow had to be restarted too many times", step=steps[i]
                        ) from None
                    self.checks.outputs.clear()
                    for r in self.reports.values():
                        r.status = "not_run"
                    await self.session.goto(self.cap.implementation.entry.route)
                    i = 0
            await self._verify_success()
            return "succeeded", None, None, None
        except _Stop as s:
            return s.status, s.outcome, s.error, s.parked
        except SessionLost as e:
            err = FailureInfo(code="SESSION_LOST", message=f"the live session was lost: {e}", retryable=True)
            return "failed", None, err, None
        except AuthFailed as e:
            err = FailureInfo(
                code="AUTH_FAILED",
                message=str(e),
                reason=e.reason,
                retryable=e.reason == "timeout",
                hint="check the tenant's credentials / account status" if e.reason != "timeout" else None,
            )
            return "failed", None, err, None

    # ------------------------------------------------------------------ steps
    async def _run_step(self, step: Step) -> None:
        report = self.reports[step.id]
        t0 = time.monotonic()
        self.guard.step_id = step.id
        self.session.dialog_expectation = step.on_dialog
        self.recorder.event(
            "step_started", step_id=step.id, intent=step.intent, action=step.action, effect=step.effect
        )
        try:
            if step.action in ("extract", "extract_table"):
                res = await self._acquire(step)
                if res is None:
                    report.status = "skipped"
                    return
                await self._extract(step, res)
                report.attempts, report.status = report.attempts + 1, "done"
                report.strategy = f"{res.strategy['kind']}#{res.index}"
                return
            while True:
                report.attempts += 1
                res = await self._acquire(step)
                if res is None:
                    report.status = "skipped"
                    return
                outcome = await self._act(step, res)
                if outcome == "approved":
                    report.attempts -= 1  # re-resolving after an approval is not a failed attempt
                    continue
                if outcome == "retry":
                    if report.attempts >= 3:
                        raise await self._fail(
                            "TIMEOUT", "the control never became actionable", step=step, retryable=True
                        )
                    continue
                report.status = "completed_by_human" if outcome == "completed_by_human" else "done"
                report.strategy = f"{res.strategy['kind']}#{res.index}"
                return
        except _Stop as stop:
            report.status = (
                "outcome"
                if stop.status == "business_outcome"
                else "awaiting_human"
                if stop.status == "needs_human"
                else "failed"
            )
            raise
        finally:
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            self.session.dialog_expectation = None
            if report.status in ("done", "completed_by_human", "skipped"):
                self.recorder.event(
                    "step_completed",
                    step_id=step.id,
                    status=report.status,
                    strategy=report.strategy,
                    attempts=report.attempts,
                    duration_ms=report.duration_ms,
                )

    async def _act(
        self, step: Step, res: Resolution
    ) -> Literal["done", "retry", "approved", "completed_by_human"]:
        control = (await self.surface.describe(res.element)).get("control") or {}
        live_effect = self.policy.classify(step.action, control, step.effect)
        if live_effect == "commit" and step.effect != "commit":
            raise await self._fail(
                "POLICY_DENIED",
                f"step {step.id} was recorded as '{step.effect}' but now resolves to a commit-class "
                f'control ({control.get("role")} "{control.get("name")}"); refusing to act',
                step=step,
            )
        if step.effect == "commit":
            await self._check_pre(step)
            await self._check_commit_target(step, res)
            if not (self.invocation_approved or step.id in self.approved_steps):
                pre_docs = await self.surface.doc_ids()
                handoff = await self._escalate(
                    "approval",
                    step,
                    reason=f"step {step.id} is a commit ({step.intent}); it needs an explicit approval",
                    proposed=f'click {control.get("role")} "{control.get("name")}" on the verified review page',
                    allowed=["approve", "reject", "take_control", "abort"],
                )
                if handoff.kind == "approve":
                    self.approved_steps.add(step.id)
                    self.recorder.event(
                        "commit_approved",
                        step_id=step.id,
                        by=handoff.request.operator,
                        plan_sha256=self.plan.plan_sha256,
                    )
                    return "approved"  # re-resolve the target: the page may have changed while waiting
                if handoff.kind == "reject":
                    raise _Stop(
                        "business_outcome",
                        outcome=OutcomeInfo(
                            code="APPROVAL_DENIED",
                            description="An operator declined the commit step.",
                            caller_guidance="Tell the user the request was declined by staff; do not retry automatically.",
                            retry_safe=True,
                            message=handoff.request.note,
                            step_id=step.id,
                        ),
                    )
                # The operator took control (e.g. performed the commit themselves): re-check, never assume.
                return await self._after_handback(step, before_docs=pre_docs, handoff=handoff)

        before_docs = await self.surface.doc_ids()
        baseline = await self.session.red_baseline()
        try:
            async with self.controller.automated_action(self.controller.epoch):
                await self._perform(step, res)
        except ControlLost:
            return await self._implicit_handback(step, before_docs)
        except NotReady:
            return "retry"
        if step.effect == "commit":
            self.commit_dispatched = step.id
            self.recorder.event("commit_dispatched", step_id=step.id)
        return await self._post_wait(step, before_docs, baseline)

    async def _perform(self, step: Step, res: Resolution) -> None:
        el = res.element
        if step.action == "click":
            await self.surface.click(el)
        elif step.action == "fill":
            assert step.value is not None
            await self.surface.fill(el, self.checks.resolve_value(step.value))
        elif step.action == "select":
            assert step.value is not None
            await self.surface.select(el, self.checks.resolve_value(step.value))
        elif step.action == "press_key":
            assert step.key is not None
            await self.surface.press(el, step.key)
        self.recorder.event(
            "action_performed",
            step_id=step.id,
            action=step.action,
            strategy=res.strategy["kind"],
            strategy_index=res.index,
        )

    async def _extract(self, step: Step, res: Resolution) -> None:
        assert step.output is not None
        spec = self.cap.contract.outputs[step.output]
        try:
            if step.action == "extract":
                text = await self.surface.read_text(res.element)
                value: Any = parse_value(text, step.parse or "string", spec.currency)
            else:
                rows = await self.surface.read_table(res.element, step.columns or {})
                col_types = spec.columns or {}
                value = [
                    {
                        k: (parse_value(v, col_types.get(k, "string"), spec.currency) if v else None)
                        for k, v in row.items()
                    }
                    for row in rows
                ]
        except (ValueError, InvalidOperation) as e:
            raise await self._fail(
                "CHECKPOINT_FAILED", f"could not read output {step.output}: {e}", step=step
            ) from e
        self.checks.outputs[step.output] = value
        self.recorder.event(
            "output_extracted",
            step_id=step.id,
            output=step.output,
            output_type=spec.type,
            value=self.redactor.output_for_log(value, spec.sensitivity),
        )

    # ------------------------------------------------------------------ waiting
    async def _race(
        self,
        want: Callable[[], Awaitable[Any]],
        *,
        phase: Literal["pre", "post"],
        timeout_ms: int,
        baseline: dict[tuple[str, ...], set[str]] | None = None,
    ) -> tuple[str, Any]:
        """Poll until `want` yields a value, a runtime condition is detected, or time runs out."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self.surface.closed:
                raise SessionLost("the browser window was closed")
            if self.controller.state == ControlState.HUMAN:
                return "takeover", None
            try:
                hit = await self.guard.check(phase=phase, baseline=baseline)
                if hit:
                    return "hit", hit
                value = await want()
                if value:
                    return "ok", value
            except NotReady:
                pass
            if time.monotonic() >= deadline:
                return "timeout", None
            await asyncio.sleep(self.timeouts.poll_ms / 1000)

    async def _resolve(self, step: Step) -> Resolution | None:
        res, diag, present = await self.surface.resolve(
            step.target.container, strategy_dicts(step.target, self.checks.params)
        )
        self._diag = (diag, present)
        return res

    async def _acquire(self, step: Step) -> Resolution | None:
        timeout = min(step.timeout_ms, 2000) if step.optional else step.timeout_ms
        while True:
            kind, value = await self._race(lambda: self._resolve(step), phase="pre", timeout_ms=timeout)
            if kind == "ok":
                if value.index > 0:
                    primary, count = self._diag[0][0]
                    self._warn(
                        "FALLBACK_LOCATOR_USED",
                        step.id,
                        f"primary {primary['kind']} strategy matched {count} elements; used "
                        f"{value.strategy['kind']} (strategy {value.index + 1}). Possible UI drift.",
                    )
                return value
            if kind == "hit":
                if await self._handle_hit(value, step, None) == "completed_by_human":
                    return None
                continue
            if kind == "takeover":
                await self._implicit_handback(step, None)
                continue
            if step.optional:
                self.recorder.event("optional_step_skipped", step_id=step.id)
                return None
            raise await self._not_found(step)

    async def _post_wait(
        self, step: Step, before_docs: dict[tuple[str, ...], str], baseline: dict[tuple[str, ...], set[str]]
    ) -> Literal["done", "completed_by_human", "retry"]:
        async def want() -> bool:
            if not self.surface.settled(self.timeouts.settle_ms):
                return False
            for c in step.expect:
                if not await self.checks.holds(c, step_target=step.target, before_docs=before_docs):
                    return False
            return True

        while True:
            kind, value = await self._race(want, phase="post", timeout_ms=step.timeout_ms, baseline=baseline)
            if kind == "ok":
                if step.effect == "commit":
                    self.committed, self.commit_dispatched = True, None
                    self.recorder.event("commit_verified", step_id=step.id)
                return "done"
            if kind == "hit":
                if await self._handle_hit(value, step, before_docs) == "completed_by_human":
                    return "completed_by_human"
                continue
            if kind == "takeover":
                return await self._implicit_handback(step, before_docs)
            raise await self._checkpoint_timeout(step, before_docs)

    # ------------------------------------------------------------------ conditions, commits
    async def _check_pre(self, step: Step) -> None:
        for cond in step.pre:
            ok = False
            for _ in range(8):
                try:
                    if await self.checks.holds(cond, step_target=step.target):
                        ok = True
                        break
                except NotReady:
                    pass
                await asyncio.sleep(self.timeouts.poll_ms / 1000)
            if not ok:
                raise await self._fail(
                    "CHECKPOINT_FAILED",
                    "pre-commit check failed: the review page does not show "
                    f"what we are about to commit ({describe(cond)})",
                    step=step,
                    expected=describe(cond),
                    hint="nothing was committed; the review page differs from the inputs",
                )

    async def _check_commit_target(self, step: Step, res: Resolution) -> None:
        if res.index == 0:
            return
        strategies = strategy_dicts(step.target, self.checks.params)
        for other in strategies:
            if other is res.strategy:
                continue
            if await self.surface.strategies_agree(step.target.container, res.strategy, other):
                return
        raise await self._fail(
            "TARGET_NOT_FOUND",
            f"commit control for {step.id} matched only through fallback strategy "
            f"{res.strategy['kind']}; refusing to commit on a weak match",
            step=step,
            hint="re-target the step (version overlay) and re-approve",
        )

    # ------------------------------------------------------------------ runtime conditions
    async def _handle_hit(self, hit: Hit, step: Step, before_docs: dict[tuple[str, ...], str] | None) -> str:
        self.recorder.event(
            "detector_fired",
            step_id=step.id,
            detector=hit.source,
            kind=hit.kind,
            code=hit.code,
            message=hit.message,
        )
        if hit.kind == "recoverable":
            outcome = await self.guard.recover(
                hit, can_redrive=self.commit_dispatched is None and not self.committed
            )
            if outcome.status == "handled":
                return "continue"
            if outcome.status == "redrive":
                raise _Redrive()
            if outcome.status == "not_allowed":
                self.side_effect_hint = "unknown"
                handoff = await self._escalate(
                    "unrecoverable",
                    step,
                    reason=f"{hit.message} after a commit was dispatched; cannot re-drive",
                    allowed=["take_control", "abort"],
                )
                return await self._after_handback(step, before_docs, handoff)
            det = hit.detector
            code = failure_code(det.failure_code if det else None, "APP_ERROR")
            raise await self._fail(
                code,
                f"{hit.message} (persisted after bounded recovery: {outcome.detail})",
                step=step,
                retryable=code in RETRYABLE,
            )
        if hit.kind == "business_outcome":
            spec = self.cap.contract.outcomes.get(hit.code or "")
            raise _Stop(
                "business_outcome",
                outcome=OutcomeInfo(
                    code=hit.code or "UNKNOWN",
                    description=spec.description if spec else hit.message,
                    caller_guidance=spec.caller_guidance if spec else "",
                    retry_safe=spec.retry_safe if spec else True,
                    message=hit.message,
                    step_id=step.id,
                ),
            )
        if hit.kind == "human_required":
            self.side_effect_hint = hit.implies
            handoff = await self._escalate(
                "human_required", step, reason=hit.message, allowed=["take_control", "abort"]
            )
            return await self._after_handback(step, before_docs, handoff)
        code = failure_code(hit.code, "UNRECOGNIZED_STATE")
        raise await self._fail(code, hit.message, step=step, retryable=code in RETRYABLE)

    # ------------------------------------------------------------------ human in the loop
    async def _escalate(
        self,
        kind: InterventionKind,
        step: Step,
        *,
        reason: str,
        allowed: list[DecisionKind],
        proposed: str | None = None,
    ) -> Handoff:
        shot = await self.session.screenshot(f"escalation-{step.id}")
        excerpt = await self.session.excerpt()
        handoff = await self.controller.escalate(
            kind=kind,
            reason=reason,
            allowed=allowed,
            capability=self.cap.ref,
            step_id=step.id,
            step_intent=step.intent,
            proposed_action=proposed,
            screenshot=shot,
            snapshot_excerpt=excerpt,
        )
        self._record_intervention(handoff)
        if handoff.kind in ("parked", "timeout"):
            req = handoff.request
            raise _Stop(
                "needs_human",
                parked=Parked(
                    intervention_id=req.id,
                    deadline=req.deadline,
                    session_retained=False,  # this prototype ends the process; see REPORT for the durable design
                    request_path=f"interventions/{req.id}.json",
                ),
            )
        if handoff.kind == "abort":
            raise await self._fail(
                "ABORTED_BY_OPERATOR", f"operator {handoff.request.operator} aborted the run", step=step
            )
        return handoff

    def _record_intervention(self, handoff: Handoff) -> None:
        req = handoff.request
        self.interventions.append(
            InterventionRecord(
                id=req.id,
                kind=req.kind,
                reason=self.redactor.scrub_text(req.reason),
                step_id=req.step_id,
                decision=req.decision,
                operator=req.operator,
                note=req.note,
                requested_at=req.requested_at,
                resolved_at=req.resolved_at,
                human_actions=handoff.human_actions,
            )
        )

    async def _after_handback(
        self, step: Step, before_docs: dict[tuple[str, ...], str] | None, handoff: Handoff
    ) -> Literal["completed_by_human", "retry"]:
        """Re-sync after a human handed control back: never assume, re-check."""
        if step.expect and before_docs is not None:
            await self.session.wait_settled()
            if all(
                [
                    await self.checks.holds(c, step_target=step.target, before_docs=before_docs)
                    for c in step.expect
                ]
            ):
                if step.effect == "commit":
                    self.committed, self.commit_dispatched = True, None
                self.recorder.event("resynced", step_id=step.id, result="completed_by_human")
                return "completed_by_human"
        if await self._resolve(step) is not None:
            self.recorder.event("resynced", step_id=step.id, result="retry_step")
            return "retry"
        raise await self._fail(
            "RESYNC_FAILED",
            f"after hand-back neither the checkpoint of {step.id} holds nor is its control present",
            step=step,
        )

    async def _implicit_handback(
        self, step: Step, before_docs: dict[tuple[str, ...], str] | None
    ) -> Literal["completed_by_human", "retry"]:
        handoff = await self.controller.wait_for_handback()
        self._record_intervention(handoff)
        if handoff.kind == "abort":
            raise await self._fail("ABORTED_BY_OPERATOR", "operator aborted the run", step=step)
        if handoff.kind == "timeout":
            raise _Stop(
                "needs_human",
                parked=Parked(
                    intervention_id=handoff.request.id,
                    deadline=handoff.request.deadline,
                    session_retained=False,
                    request_path=f"interventions/{handoff.request.id}.json",
                ),
            )
        return await self._after_handback(step, before_docs, handoff)

    # ------------------------------------------------------------------ failures
    def _warn(self, code: str, step_id: str | None, detail: str) -> None:
        self.warnings.append(WarningRecord(code=code, step_id=step_id, detail=detail))
        self.recorder.event("warning", code=code, step_id=step_id, detail=detail)

    async def _fail(
        self,
        code: FailureCode,
        message: str,
        *,
        step: Step | None = None,
        expected: str | None = None,
        observed: str | None = None,
        hint: str | None = None,
        near_misses: list[str] | None = None,
        retryable: bool = False,
        status: str = "failed",
    ) -> _Stop:
        evidence = []
        shot = await self.session.screenshot(f"failure-{step.id if step else 'run'}")
        if shot:
            evidence.append(shot)
        evidence.append(
            self.recorder.save_text(
                f"snapshots/failure-{step.id if step else 'run'}.txt", await self.session.excerpt()
            )
        )
        err = FailureInfo(
            code=code,
            message=self.redactor.scrub_text(message),
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            expected=expected,
            observed=observed,
            retryable=retryable,
            hint=hint,
            near_misses=near_misses or [],
            evidence=evidence,
        )
        self.recorder.event("failure", **err.model_dump(exclude_none=True))
        return _Stop(status, error=err)

    async def _not_found(self, step: Step) -> _Stop:
        diag, present = self._diag
        where = "/".join(step.target.container) or "top"
        if not present:
            return await self._fail("TIMEOUT", f"container {where} never appeared", step=step, retryable=True)
        tried = [
            f"{s['kind']} { ({k: v for k, v in s.items() if k != 'kind'}) } -> {n} matches" for s, n in diag
        ]
        if diag and all(n != 1 for _, n in diag) and any(n > 1 for _, n in diag):
            return await self._fail(
                "AMBIGUOUS_TARGET",
                f"every strategy for {step.id} matched several elements",
                step=step,
                observed="; ".join(tried),
            )
        misses = await self.surface.near_misses(
            step.target.container, strategy_dicts(step.target, self.checks.params)[0]
        )
        near = [
            f'{m["role"]} "{m["name"] or m["label"]}" (similarity {m["score"]:.2f})'
            for m in misses
            if m.get("score", 0) > 0
        ]
        expected = " OR ".join(
            f"{s['kind']} { ({k: v for k, v in s.items() if k != 'kind'}) }" for s, _ in diag
        )
        return await self._fail(
            "TARGET_NOT_FOUND",
            f"no strategy found the control for step {step.id} ({step.intent}) in {where}",
            step=step,
            expected=expected,
            observed="; ".join(tried),
            near_misses=near,
            hint=(
                "the page loaded but the control is missing: likely UI drift (renamed/moved). "
                f"Nearest candidates: {', '.join(near[:3]) or 'none'}. Re-target step '{step.id}' in a version overlay."
            ),
        )

    async def _checkpoint_timeout(self, step: Step, before_docs: dict[tuple[str, ...], str]) -> _Stop:
        expected = "; ".join(describe(c) for c in step.expect) or "(no checkpoint)"
        now = await self.surface.doc_ids()
        changed = [k for k, v in now.items() if before_docs.get(k) != v]
        if "document_changed" in _expect_kinds(step) and not changed:
            return await self._fail(
                "TIMEOUT",
                f"the page did not change after {step.action} on {step.id}",
                step=step,
                expected=expected,
                observed="no new document",
                retryable=True,
            )
        if changed:
            excerpt = (await self.session.excerpt())[:400]
            return await self._fail(
                "UNRECOGNIZED_STATE",
                f"after {step.id} the app showed a page we do not recognise",
                step=step,
                expected=expected,
                observed=excerpt,
            )
        return await self._fail(
            "CHECKPOINT_FAILED", f"checkpoint for {step.id} did not hold", step=step, expected=expected
        )

    async def _verify_success(self) -> None:
        success = self.cap.implementation.success
        for _ in range(12):
            try:
                if await self.checks.holds(success):
                    self.recorder.event("success_verified", condition=describe(success))
                    return
            except NotReady:
                pass
            await asyncio.sleep(self.timeouts.poll_ms / 1000)
        raise await self._fail(
            "CHECKPOINT_FAILED", "the final success condition did not hold", expected=describe(success)
        )

    def side_effect(self) -> str:
        if self.cap.contract.effects == "read_only":
            return "none"
        if self.committed:
            return "committed"
        if self.commit_dispatched:
            return self.side_effect_hint or "unknown"
        return "not_committed"


# ---------------------------------------------------------------------------------- entry point


async def run_replay(
    cap: Capability,
    *,
    tenant_id: str,
    inputs: dict[str, str],
    approve: bool = False,
    headed: bool = False,
    operator: OperatorMode = "none",
    operator_hook: OperatorHook | None = None,
    use_overlays: bool = True,
    runs_root: Path | None = None,
    console_port: int = 8765,
    record_video: bool = False,
    tenant_overrides: dict[str, Any] | None = None,
) -> RunResult:
    tenant = load_tenant(tenant_id)
    if tenant_overrides:
        tenant = tenant.model_copy(update=tenant_overrides)
    app = load_app_profile(tenant.app)
    policy_cfg = load_policy(tenant.policy)
    overlays = load_overlays()
    redactor = Redactor(app.sensitive_labels)
    recorder = RunRecorder(runs_root or repo_root() / "runs", "replay", redactor)
    started, t0 = utcnow(), time.monotonic()

    def finish(result: RunResult) -> RunResult:
        result.finished_at = utcnow()
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        result.evidence_dir = str(recorder.dir)
        persisted = result.model_copy(deep=True)
        if persisted.outputs:
            specs = cap.contract.outputs
            persisted.outputs = {
                k: redactor.output_for_log(v, specs[k].sensitivity) for k, v in persisted.outputs.items()
            }
        recorder.save_json("result.json", persisted.model_dump(mode="json"))
        recorder.event(
            "run_finished",
            status=result.status,
            side_effect=result.side_effect,
            code=(result.outcome.code if result.outcome else result.error.code if result.error else None),
        )
        recorder.close()
        return result

    base = RunResult(
        run_id=recorder.run_id,
        mode="replay",
        capability=cap.ref,
        tenant=tenant.id,
        status="rejected",
        started_at=started,
    )
    recorder.event(
        "run_started",
        mode="replay",
        capability=cap.ref,
        tenant=tenant.id,
        environment=tenant.environment,
        approve=approve,
        operator=operator,
    )

    def reject(code: str, message: str) -> RunResult:
        base.error = FailureInfo(code=code, message=message)  # type: ignore[arg-type]
        base.status = "rejected"
        recorder.event("rejected", code=code, message=message)
        return finish(base)

    # ---- pre-flight: nothing on the UI is touched --------------------------------
    if cap.implementation.app.product != tenant.app or not version_in(
        cap.implementation.app.versions, tenant.product_version
    ):
        return reject(
            "INCOMPATIBLE_VERSION",
            f"{cap.ref} supports {cap.implementation.app.product} "
            f"{cap.implementation.app.versions}; tenant {tenant.id} runs {tenant.app} {tenant.product_version}",
        )
    try:
        plan = resolve_plan(cap, tenant, app, policy_cfg, overlays, use_overlays=use_overlays)
    except ValueError as e:
        return reject("CAPABILITY_INVALID", str(e))
    base.effective_plan_sha256, base.overlays_applied = plan.plan_sha256, plan.overlays_applied
    clean, errors = validate_inputs(cap, inputs)
    if errors:
        return reject("INPUT_INVALID", "; ".join(errors))
    for name, value in clean.items():
        redactor.add_param(name, value, cap.contract.inputs[name].sensitivity)
    engine_policy = PolicyEngine(policy_cfg, tenant, app)
    problems = engine_policy.preflight(cap)
    if problems:
        code, _ = problems[0]
        return reject(code, "; ".join(msg for _, msg in problems))
    recorder.event(
        "preflight_passed",
        plan_sha256=plan.plan_sha256,
        overlays=plan.overlays_applied,
        inputs={k: redactor.scrub_text(v) for k, v in clean.items()},
    )

    # ---- execution -------------------------------------------------------------
    controller = SessionController(
        recorder,
        redactor,
        operator_available=operator != "none",
        handoff_timeout_s=app.timeouts.handoff_ms / 1000,
    )
    session = RuntimeSession(
        tenant=tenant,
        app=app,
        policy=engine_policy,
        redactor=redactor,
        recorder=recorder,
        controller=controller,
        headed=headed or operator == "console",
        record_video=record_video,
    )
    engine = ReplayEngine(
        plan,
        session=session,
        controller=controller,
        recorder=recorder,
        redactor=redactor,
        policy=engine_policy,
        inputs=clean,
        tenant_version=tenant.product_version,
        invocation_approved=approve,
    )
    console = None
    hook_task: asyncio.Task[None] | None = None
    try:
        await session.start()
        if operator == "console":
            from .operator_console import OperatorConsole

            console = OperatorConsole(controller, recorder, port=console_port)
            await console.start()
        if operator_hook is not None:
            hook_task = asyncio.create_task(operator_hook(controller, session))
        status, outcome, error, parked = await engine.run()
    finally:
        if hook_task:
            hook_task.cancel()
        await session.close()
        controller.close()
        if console:
            await console.stop()

    side_effect = engine.side_effect()
    result = base.model_copy(
        update={
            "status": status,
            "outputs": dict(engine.checks.outputs) if status == "succeeded" else None,
            "outcome": outcome,
            "error": error,
            "side_effect": side_effect,
            "retry_safe": side_effect in ("none", "not_committed")
            and (outcome.retry_safe if outcome else True),
            "recoveries": engine.guard.recoveries,
            "warnings": engine.warnings,
            "interventions": engine.interventions,
            "parked": parked,
            "steps": list(engine.reports.values()),
        }
    )
    return finish(result)
