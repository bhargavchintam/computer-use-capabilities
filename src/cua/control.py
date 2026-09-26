"""Control transfer between automation and a human operator on the same live session.

State machine::

    AUTOMATION --escalate--> AWAITING_HUMAN --take_control--> HUMAN
        ^                        |   |                          |
        |<------approve/reject---+   +--abort--> CLOSED <--abort-+
        |<--------------------------hand_back-------------------+

* The lease ``epoch`` increases on every transfer. Every automated action runs
  inside ``automated_action(epoch_seen)`` and refuses to act if control moved
  since the decision was made (stale decision), so automation can never fight
  a human for the session.
* Human input that arrives while automation holds the lease (someone grabbed
  the window) is an implicit takeover: automation yields and waits for hand-back.
* Everything the human does is captured and recorded; values typed into
  secret fields (password, PIN) are never captured.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .evidence import RunRecorder, utcnow
from .models.results import HumanAction
from .redaction import Redactor

InterventionKind = Literal["approval", "stuck", "unrecoverable", "human_required", "implicit_takeover"]
DecisionKind = Literal["approve", "reject", "take_control", "hand_back", "abort"]
ResolutionKind = Literal["approve", "reject", "hand_back", "abort", "timeout", "parked"]


class ControlState(StrEnum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    CLOSED = "closed"


class ControlLost(Exception):
    """Automation tried to act without holding the lease (or on a stale decision)."""


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    kind: InterventionKind
    reason: str
    capability: str | None = None
    goal: str | None = None
    step_id: str | None = None
    step_intent: str | None = None
    proposed_action: str | None = None
    screenshot: str | None = None
    snapshot_excerpt: str | None = None
    allowed_decisions: list[DecisionKind]
    requested_at: str
    deadline: str
    status: Literal["pending", "active", "resolved", "expired"] = "pending"
    decision: DecisionKind | None = None
    operator: str | None = None
    note: str | None = None
    resolved_at: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)


@dataclass
class Resolution:
    request: InterventionRequest
    kind: ResolutionKind
    human_actions: list[HumanAction] = field(default_factory=list)
    captured: list[dict[str, Any]] = field(default_factory=list)  # redacted describe payloads, for traces


class SessionController:
    def __init__(
        self,
        recorder: RunRecorder,
        redactor: Redactor,
        *,
        operator_available: bool,
        handoff_timeout_s: float = 900.0,
    ) -> None:
        self.recorder = recorder
        self.redactor = redactor
        self.operator_available = operator_available
        self.handoff_timeout_s = handoff_timeout_s
        self.state = ControlState.AUTOMATION
        self.holder = "automation"
        self.epoch = 0
        self.requests: dict[str, InterventionRequest] = {}
        self.active: InterventionRequest | None = None
        self.action_in_progress = False
        self._last_action_end = 0.0
        self._decisions: asyncio.Queue[tuple[str, DecisionKind, str, str | None]] = asyncio.Queue()
        self._human_actions: list[HumanAction] = []
        self._captured: list[dict[str, Any]] = []
        self._pending_capture: tuple[dict[str, Any], list[str]] | None = None
        # Hooks: the surface brings the window forward; operator surfaces get notified.
        self.on_take_control: Callable[[], Awaitable[None]] | None = None
        self.on_request: list[Callable[[InterventionRequest], None]] = []
        recorder.context = lambda: {"actor": self.actor, "epoch": self.epoch}

    # ------------------------------------------------------------------ lease
    @property
    def actor(self) -> str:
        return f"human:{self.holder}" if self.state == ControlState.HUMAN else "automation"

    def _transfer(self, to: ControlState, holder: str, reason: str) -> None:
        old = self.state
        self.state, self.holder = to, holder
        self.epoch += 1
        self.recorder.event(
            "control_transferred", from_state=old.value, to_state=to.value, holder=holder, reason=reason
        )

    def assert_automation(self, epoch_seen: int | None = None) -> None:
        if self.state != ControlState.AUTOMATION:
            raise ControlLost(f"control is held by {self.holder} ({self.state.value})")
        if epoch_seen is not None and epoch_seen != self.epoch:
            raise ControlLost("control changed hands since this decision was made; re-observe")

    @asynccontextmanager
    async def automated_action(self, epoch_seen: int | None = None) -> AsyncIterator[None]:
        self.assert_automation(epoch_seen)
        self.action_in_progress = True
        try:
            yield
        finally:
            self.action_in_progress = False
            self._last_action_end = time.monotonic()

    def close(self) -> None:
        if self.state != ControlState.CLOSED:
            self._transfer(ControlState.CLOSED, "nobody", "run finished")

    # ------------------------------------------------------------------ interventions
    def _new_request(self, **kw: Any) -> InterventionRequest:
        now = datetime.now(UTC)
        req = InterventionRequest(
            id="iv-" + secrets.token_hex(4),
            run_id=self.recorder.run_id,
            requested_at=utcnow(),
            deadline=(now + timedelta(seconds=self.handoff_timeout_s))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            **kw,
        )
        self.requests[req.id] = req
        return req

    def _persist(self, req: InterventionRequest) -> str:
        return self.recorder.save_json(f"interventions/{req.id}.json", req.model_dump(mode="json"))

    async def escalate(
        self,
        *,
        kind: InterventionKind,
        reason: str,
        allowed: list[DecisionKind],
        capability: str | None = None,
        goal: str | None = None,
        step_id: str | None = None,
        step_intent: str | None = None,
        proposed_action: str | None = None,
        screenshot: str | None = None,
        snapshot_excerpt: str | None = None,
    ) -> Resolution:
        req = self._new_request(
            kind=kind,
            reason=reason,
            allowed_decisions=allowed,
            capability=capability,
            goal=goal,
            step_id=step_id,
            step_intent=step_intent,
            proposed_action=proposed_action,
            screenshot=screenshot,
            snapshot_excerpt=snapshot_excerpt,
        )
        self._persist(req)
        self.recorder.event(
            "intervention_requested",
            intervention_id=req.id,
            kind=kind,
            reason=reason,
            step_id=step_id,
            proposed_action=proposed_action,
            routed_to="operator" if self.operator_available else "queue",
        )
        if not self.operator_available:
            return Resolution(req, "parked")
        self.active = req
        self._transfer(ControlState.AWAITING_HUMAN, "operator-queue", f"{kind}: {reason}")
        for notify in self.on_request:
            notify(req)
        return await self._await_resolution(req)

    async def wait_for_handback(self) -> Resolution:
        """Called when automation discovers a human already holds control (implicit takeover)."""
        if self.active is None:
            raise RuntimeError("no active intervention")
        return await self._await_resolution(self.active)

    async def _await_resolution(self, req: InterventionRequest) -> Resolution:
        deadline = time.monotonic() + self.handoff_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            try:
                rid, decision, operator, note = await asyncio.wait_for(
                    self._decisions.get(), timeout=max(0.1, remaining)
                )
            except TimeoutError:
                req.status = "expired"
                self._persist(req)
                self.recorder.event("intervention_expired", intervention_id=req.id)
                return Resolution(req, "timeout", list(self._human_actions), list(self._captured))
            if rid != req.id:
                continue
            if decision == "take_control" and self.state == ControlState.AWAITING_HUMAN:
                req.status, req.operator = "active", operator
                self._human_actions, self._captured = [], []
                self._transfer(ControlState.HUMAN, operator, "operator took control of the live session")
                if self.on_take_control:
                    with contextlib.suppress(Exception):  # bringing a window forward is best-effort
                        await self.on_take_control()
                self._persist(req)
                continue
            if decision in ("approve", "reject") and self.state == ControlState.AWAITING_HUMAN:
                return self._finish(req, decision, operator, note, ControlState.AUTOMATION)
            if decision == "hand_back" and self.state == ControlState.HUMAN:
                return self._finish(req, decision, operator, note, ControlState.AUTOMATION)
            if decision == "abort":
                return self._finish(req, decision, operator, note, ControlState.CLOSED)
            self.recorder.event(
                "decision_ignored", intervention_id=req.id, decision=decision, state=self.state.value
            )

    def _finish(
        self,
        req: InterventionRequest,
        decision: DecisionKind,
        operator: str,
        note: str | None,
        to: ControlState,
    ) -> Resolution:
        req.status, req.decision, req.operator = "resolved", decision, operator
        req.note = self.redactor.scrub_text(note) if note else None
        req.resolved_at = utcnow()
        req.human_actions = list(self._human_actions)
        self._persist(req)
        self.recorder.event(
            "intervention_resolved",
            intervention_id=req.id,
            decision=decision,
            operator=operator,
            note=req.note,
            human_actions=len(self._human_actions),
        )
        self._transfer(
            to, "automation" if to == ControlState.AUTOMATION else operator, f"operator decision: {decision}"
        )
        self.active = None
        kind: ResolutionKind = decision  # type: ignore[assignment]
        return Resolution(req, kind, list(self._human_actions), list(self._captured))

    def decide(
        self, request_id: str, decision: DecisionKind, operator: str, note: str | None = None
    ) -> tuple[bool, str]:
        req = self.requests.get(request_id)
        if req is None:
            return False, "unknown intervention"
        if req.status in ("resolved", "expired"):
            return False, f"intervention already {req.status}"
        if decision not in req.allowed_decisions and decision != "hand_back":
            return False, f"decision {decision} not allowed for this intervention"
        self._decisions.put_nowait((request_id, decision, operator, note))
        return True, "accepted"

    # ------------------------------------------------------------------ human input capture
    def on_capture(self, payload: dict[str, Any], container: list[str]) -> None:
        if self.state == ControlState.HUMAN:
            self._record_human(payload, container)
        elif self.state == ControlState.AWAITING_HUMAN and self.active is not None:
            # The operator started working in the window without pressing "take control".
            self._decisions.put_nowait((self.active.id, "take_control", "local-operator", None))
            self._pending_capture = (payload, container)
            asyncio.get_running_loop().call_soon(self._flush_pending)
        elif (
            self.state == ControlState.AUTOMATION
            and not self.action_in_progress
            and time.monotonic() - self._last_action_end > 1.0
        ):
            self._implicit_takeover(payload, container)

    def _flush_pending(self) -> None:
        pending = self._pending_capture
        if pending and self.state == ControlState.HUMAN:
            self._record_human(*pending)
            self._pending_capture = None

    def _implicit_takeover(self, payload: dict[str, Any], container: list[str]) -> None:
        req = self._new_request(
            kind="implicit_takeover",
            reason="human input detected in the live window while automation held control",
            allowed_decisions=["hand_back", "abort"],
            status="active",
            operator="local-operator",
        )
        self.active = req
        self._human_actions, self._captured = [], []
        self._transfer(ControlState.HUMAN, "local-operator", "implicit takeover: human input detected")
        self._persist(req)
        for notify in self.on_request:
            notify(req)
        self._record_human(payload, container)

    def _record_human(self, payload: dict[str, Any], container: list[str]) -> None:
        d = payload.get("describe") or {}
        what = d.get("role") or d.get("tag") or "element"
        label = d.get("name") or d.get("label") or ""
        target = self.redactor.scrub_text(f'{what} "{label}"' if label else what)
        secret = bool(payload.get("secret")) or bool((d.get("control") or {}).get("secret"))
        raw_value = None if secret else payload.get("value")
        value = (
            "<not captured: secret field>"
            if secret and payload.get("type") == "change"
            else (self.redactor.scrub_text(str(raw_value)) if raw_value else None)
        )
        action = HumanAction(
            type=str(payload.get("type")), container=container, target=target, value=value, at=utcnow()
        )
        self._human_actions.append(action)
        # In memory only: the raw (non-secret) value lets discovery map a typed input back to
        # its {{param}}. Anything persisted goes through the recorder, which scrubs it.
        self._captured.append(
            {
                "payload": {k: v for k, v in payload.items() if k != "value"},
                "container": container,
                "value": raw_value,
            }
        )
        self.recorder.event("human_action", action=action.model_dump(mode="json"))
