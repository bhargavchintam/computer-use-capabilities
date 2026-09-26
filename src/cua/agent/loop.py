"""The discovery loop: observe -> decide (LLM) -> act, until the goal is met or a stop condition hits.

Every tool call passes through: policy (allowlist + effect class) -> commit approval
(human) -> control lease (epoch check) -> real input events -> runtime guard (known
interruptions recovered, never recorded) -> fresh observation. Each action is grounded
into validated candidate targets at the moment it happens and appended to the trace.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..checks import Checks, money_variants
from ..control import ControlLost, SessionController
from ..control import Resolution as Handoff
from ..evidence import RunRecorder
from ..models import AppProfile, GoalSpec
from ..models.goal import PLACEHOLDER
from ..models.results import InterventionRecord
from ..observation import marks, render
from ..policy import PolicyEngine
from ..redaction import Redactor
from ..replay import parse_value
from ..runtime import RuntimeGuard, RuntimeSession
from ..surface.base import NotReady, PageSnapshot, SessionLost, StaleRef
from ..trace import PageState, TraceStep
from .llm import LLMClient, LLMTurn
from .prompts import SYSTEM, goal_message
from .tools import tool_definitions

AgentStatus = Literal["succeeded", "business_outcome", "needs_human", "failed"]


@dataclass
class ToolResult:
    blocks: list[dict[str, Any]]
    is_error: bool = False
    note: str | None = None  # runtime/human note, sent as a system message


@dataclass
class AgentOutcome:
    status: AgentStatus
    code: str | None = None
    message: str | None = None
    summary: str | None = None
    success: dict[str, Any] | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    trace: list[TraceStep] = field(default_factory=list)
    interventions: list[InterventionRecord] = field(default_factory=list)
    turns: int = 0
    models: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    commit_approved_by: str | None = None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower().replace("#", " number ")).strip("_")


class DiscoveryAgent:
    def __init__(
        self,
        *,
        spec: GoalSpec,
        session: RuntimeSession,
        controller: SessionController,
        recorder: RunRecorder,
        redactor: Redactor,
        policy: PolicyEngine,
        llm: LLMClient,
        app: AppProfile,
        entry_route: str,
        max_turns: int = 40,
        max_minutes: float = 15.0,
        vision: bool = True,
    ) -> None:
        self.spec = spec
        self.session = session
        self.surface = session.surface
        self.controller = controller
        self.recorder = recorder
        self.redactor = redactor
        self.policy = policy
        self.llm = llm
        self.app = app
        self.entry_route = entry_route
        self.max_turns = max_turns
        self.max_seconds = max_minutes * 60
        self.vision = vision
        self.checks = Checks(
            self.surface, params=spec.sample_inputs, input_specs=spec.inputs, secrets=session.secrets
        )
        self.guard = RuntimeGuard(session, self.checks, RuntimeGuard.outcome_rules_from_messages(app))
        self.tools = tool_definitions(list(spec.outputs))
        self.messages: list[dict[str, Any]] = []
        self.trace: list[TraceStep] = []
        self.outputs: dict[str, Any] = {}
        self.interventions: list[InterventionRecord] = []
        self.snap: PageSnapshot | None = None
        self.obs_epoch = 0
        self.obs_count = 0
        self.turns = 0
        self.models: list[str] = []
        self.usage: dict[str, int] = {}
        self.final: AgentOutcome | None = None
        self.commit_approved_by: str | None = None
        self.committed = False
        # stuck detection
        self.consecutive_errors = 0
        self.policy_denials = 0
        self.no_progress = 0
        self.nudges = 0
        self.last_actions: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ observation
    async def _state(self) -> PageState:
        return PageState.from_probe(await self.surface.page_states())

    async def observe(self) -> list[dict[str, Any]]:
        await self.session.wait_settled()
        self.snap = await self.surface.snapshot()
        self.obs_epoch = self.controller.epoch
        self.obs_count += 1
        text = render(self.snap, self.redactor, for_model=True)
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": f"<observation n={self.obs_count}>\n{text}\n</observation>"}
        ]
        self.recorder.save_text(f"snapshots/obs-{self.obs_count:02d}.txt", text)
        png = None
        if self.vision:
            png = await self.surface.screenshot(
                self.app.sensitive_labels,
                marks=marks(self.snap),
                sensitive_values=self.redactor.identifying_values(),
            )
        if png:
            self.recorder.save_screenshot(png, f"obs-{self.obs_count:02d}")
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode(),
                    },
                }
            )
        self.recorder.event(
            "observation",
            n=self.obs_count,
            signature=self.snap.signature(),
            frames=[
                {"container": f.container, "path": f.url_path, "items": len(f.items)}
                for f in self.snap.frames
            ],
        )
        return blocks

    # ------------------------------------------------------------------ main loop
    async def run(self) -> AgentOutcome:
        started = time.monotonic()
        first = await self.observe()
        intro = goal_message(
            self.spec.goal,
            {k: v.description for k, v in self.spec.inputs.items()},
            {k: f"{v.type}: {v.description}" for k, v in self.spec.outputs.items()},
        )
        self.messages = [{"role": "user", "content": [{"type": "text", "text": intro}, *first]}]
        while self.final is None:
            if self.turns >= self.max_turns or time.monotonic() - started > self.max_seconds:
                await self._stuck(f"budget exhausted ({self.turns} turns)", terminal=True)
                break
            try:
                turn = await self.llm.step(system=SYSTEM, tools=self.tools, messages=self.messages)
            except Exception as e:  # noqa: BLE001 - API errors end the run with evidence
                self.recorder.event("llm_error", error=type(e).__name__, detail=str(e)[:400])
                self._finish("failed", code="LLM_ERROR", message=f"{type(e).__name__}: {str(e)[:300]}")
                break
            self._log_turn(turn)
            self.messages.append({"role": "assistant", "content": turn.content})
            if turn.stop_reason == "refusal":
                self._finish("failed", code="MODEL_REFUSED", message="the model declined to continue")
                break
            uses = [b for b in turn.content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                self.nudges += 1
                if self.nudges > 2:
                    await self._stuck("the model stopped calling tools")
                    continue
                self.messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Call exactly one tool to continue (finish, report_outcome or request_human when appropriate).",
                            }
                        ],
                    }
                )
                continue
            use = uses[0]
            result = await self._execute(use.name, dict(use.input or {}))
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": result.blocks,
                            "is_error": result.is_error,
                        }
                    ],
                }
            )
            if result.note:
                self._note(result.note)
            if self.final is None:
                await self._check_stuck()
        assert self.final is not None
        self.final.trace = self.trace
        self.final.outputs = self.outputs
        self.final.interventions = self.interventions
        self.final.turns = self.turns
        self.final.models = self.models
        self.final.usage = self.usage
        self.final.commit_approved_by = self.commit_approved_by
        return self.final

    def _log_turn(self, turn: LLMTurn) -> None:
        self.turns += 1
        if turn.model not in self.models:
            self.models.append(turn.model)
        for k, v in turn.usage.items():
            if isinstance(v, int):
                self.usage[k] = self.usage.get(k, 0) + v
        self.recorder.event(
            "llm_turn",
            turn=self.turns,
            message_id=turn.id,
            model=turn.model,
            stop_reason=turn.stop_reason,
            usage={k: v for k, v in turn.usage.items() if isinstance(v, int)},
            thinking_summary=self.redactor.scrub_text(turn.thinking[:1200], for_model=True)
            if turn.thinking
            else None,
        )

    def _note(self, text: str) -> None:
        if self.llm.supports_system_messages:
            self.messages.append({"role": "system", "content": text})
        else:
            self.messages[-1]["content"].append({"type": "text", "text": f"[runtime note] {text}"})

    def _finish(self, status: AgentStatus, **kw: Any) -> None:
        self.final = AgentOutcome(status=status, **kw)
        self.recorder.event(
            "discovery_finished", status=status, code=kw.get("code"), message=kw.get("message")
        )

    # ------------------------------------------------------------------ tool dispatch
    async def _execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.recorder.event(
            "agent_decision",
            turn=self.turns,
            tool=name,
            args={k: v for k, v in args.items() if k != "reason"},
            reason=args.get("reason"),
        )
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return await self._error(f"unknown tool {name}")
            result: ToolResult = await handler(args)
            if not result.is_error:
                self.consecutive_errors = 0
            return result
        except StaleRef as e:
            return await self._error(str(e), fresh=True)
        except NotReady:
            return await self._error(
                "the page was changing during the action; here is a fresh observation", fresh=True
            )
        except ControlLost:
            handoff = await self.controller.wait_for_handback()
            return await self._after_handoff(handoff)
        except SessionLost as e:
            self._finish("failed", code="SESSION_LOST", message=str(e))
            return ToolResult([{"type": "text", "text": "The session was lost."}], True)

    async def _error(self, message: str, *, fresh: bool = False) -> ToolResult:
        self.consecutive_errors += 1
        self.recorder.event("tool_error", message=self.redactor.scrub_text(message))
        blocks: list[dict[str, Any]] = [{"type": "text", "text": f"Error: {message}"}]
        if fresh:
            blocks += await self.observe()
        return ToolResult(blocks, True)

    # ------------------------------------------------------------------ interactions
    async def _tool_click(self, a: dict[str, Any]) -> ToolResult:
        return await self._interact("click", a)

    async def _tool_type_text(self, a: dict[str, Any]) -> ToolResult:
        return await self._interact("fill", a, text=a.get("text", ""))

    async def _tool_select_option(self, a: dict[str, Any]) -> ToolResult:
        return await self._interact("select", a, text=a.get("option", ""))

    async def _tool_press_key(self, a: dict[str, Any]) -> ToolResult:
        return await self._interact("press_key", a, key=a.get("key"))

    def _substitute(self, text: str) -> str:
        names = PLACEHOLDER.findall(text)
        if not names:
            return text
        if PLACEHOLDER.fullmatch(text.strip()) is None:
            raise ValueError("type exactly one placeholder, e.g. {{member_number}}, with no other text")
        name = names[0]
        if name not in self.spec.sample_inputs:
            raise ValueError(
                f"unknown input placeholder {{{{{name}}}}}; inputs are {sorted(self.spec.sample_inputs)}"
            )
        return self.spec.sample_inputs[name]

    async def _interact(
        self, action: str, a: dict[str, Any], *, text: str | None = None, key: str | None = None
    ) -> ToolResult:
        assert self.snap is not None
        ref = a.get("ref", "")
        _, el = await self.surface.element_for_ref(self.snap, ref)
        d = await self.surface.describe(el)
        control = d.get("control") or {}
        allowed = self.policy.action_allowed(action)
        if not allowed.allowed:
            self.policy_denials += 1
            return await self._error(allowed.reason)
        if control.get("secret") and text is not None and not PLACEHOLDER.search(text):
            self.policy_denials += 1
            return await self._error(
                "this is a credential/secret field; the runtime handles sign-on, never type secrets"
            )
        try:
            value = self._substitute(text) if text is not None else None
        except ValueError as e:
            return await self._error(str(e))
        effect = self.policy.classify(action, control)
        approved_by = None
        if effect == "commit":
            decision = await self._approve_commit(control, a)
            if isinstance(decision, ToolResult):
                return decision
            approved_by = decision
        before = await self._state()
        baseline = await self.session.red_baseline()
        container = list(self.surface.container_of(await self._frame_of(ref)))
        async with self.controller.automated_action(self.obs_epoch):
            if action == "click":
                await self.surface.click(el)
            elif action == "fill":
                await self.surface.fill(el, value or "")
            elif action == "select":
                await self.surface.select(el, value or "")
            else:
                await self.surface.press(el, key or "Enter")
        if effect == "commit":
            self.committed = True
        notes = await self._settle_and_guard(baseline)
        blocks = await self.observe()
        after = await self._state()
        step = TraceStep(
            index=len(self.trace),
            actor="agent",
            action=action,
            container=container,
            describe=d,
            reason=self.redactor.scrub_text(a.get("reason", ""), for_model=True),
            effect=effect,
            value_text=text,
            key=key,
            before=before,
            after=after,
            approved_by=approved_by,
        )
        self.trace.append(step)
        self.recorder.event("step_grounded", **step.summary())
        self._track_progress(action, d, before, after)
        head = "Done." + (" " + " ".join(notes) if notes else "")
        return ToolResult([{"type": "text", "text": head}, *blocks])

    async def _frame_of(self, ref: str) -> Any:
        assert self.snap is not None
        container, _ = self.snap.ref_index[ref]
        return self.surface.frame_for(list(container))

    async def _approve_commit(self, control: dict[str, Any], a: dict[str, Any]) -> str | ToolResult:
        what = f'{control.get("role")} "{control.get("name")}"'
        if not self.controller.operator_available:
            self.policy_denials += 1
            return await self._error(
                f"{what} commits a change and needs a human approval, but no operator is connected. "
                "Stop with report_outcome(code='APPROVAL_UNAVAILABLE') or request_human."
            )
        shot = await self.session.screenshot("approval")
        handoff = await self.controller.escalate(
            kind="approval",
            reason=f"discovery wants to perform a commit: {what}",
            proposed_action=f"{what}: {self.redactor.scrub_text(a.get('reason', ''))}",
            allowed=["approve", "reject", "take_control", "abort"],
            goal=self.spec.goal,
            screenshot=shot,
            snapshot_excerpt=await self.session.excerpt(),
        )
        self._record(handoff)
        if handoff.kind == "approve":
            self.obs_epoch = self.controller.epoch  # the approval continues this exact decision
            self.commit_approved_by = handoff.request.operator
            return handoff.request.operator or "operator"
        if handoff.kind == "reject":
            return ToolResult(
                [
                    {
                        "type": "text",
                        "text": "A human operator rejected this commit"
                        + (f" ({handoff.request.note})" if handoff.request.note else "")
                        + ". Do not retry it. Call report_outcome with code APPROVAL_DENIED.",
                    }
                ],
                True,
            )
        return await self._after_handoff(handoff)

    async def _settle_and_guard(self, baseline: dict[tuple[str, ...], set[str]]) -> list[str]:
        """Recover known interruptions (never recorded as steps); report the rest to the model."""
        await self.session.wait_settled()
        notes: list[str] = []
        for _ in range(6):
            hit = await self.guard.check(phase="post", baseline=baseline)
            if hit is None:
                break
            self.recorder.event(
                "detector_fired", detector=hit.source, kind=hit.kind, code=hit.code, message=hit.message
            )
            if hit.kind == "recoverable":
                outcome = await self.guard.recover(hit, can_redrive=not self.committed)
                if outcome.status == "handled":
                    notes.append(
                        f"(The runtime handled an interruption automatically: {hit.detector.description if hit.detector else hit.source}.)"
                    )
                    continue
                if outcome.status == "redrive":
                    for s in self.trace:
                        s.superseded = True
                    await self.session.goto(self.entry_route)
                    notes.append(
                        "(The runtime had to sign on again / restart after an interruption and returned to the "
                        "home screen. Repeat the steps needed to reach the goal.)"
                    )
                    break
                notes.append(f"(The application keeps failing: {hit.message}.)")
                break
            if hit.kind == "human_required":
                handoff = await self.controller.escalate(
                    kind="human_required",
                    reason=hit.message,
                    allowed=["take_control", "abort"],
                    goal=self.spec.goal,
                    screenshot=await self.session.screenshot("human-required"),
                    snapshot_excerpt=await self.session.excerpt(),
                )
                self._record(handoff)
                result = await self._after_handoff(handoff)
                if result.note:
                    notes.append(result.note)
                continue
            label = f"known outcome {hit.code}" if hit.kind == "business_outcome" else "unrecognized message"
            notes.append(f"(Application message, {label}: {hit.message})")
            break
        return notes

    # ------------------------------------------------------------------ outputs & terminal tools
    async def _tool_record_output(self, a: dict[str, Any]) -> ToolResult:
        assert self.snap is not None
        name = a.get("name", "")
        spec = self.spec.outputs.get(name)
        if spec is None:
            return await self._error(f"{name!r} is not a requested output")
        frame, el = await self.surface.element_for_ref(self.snap, a.get("ref", ""))
        container = self.surface.container_of(frame)
        columns: dict[str, str] | None = None
        if spec.type == "table":
            handle = await el.evaluate_handle("(e) => e.tagName === 'TABLE' ? e : e.closest('table')")
            table = handle.as_element()
            if table is None:
                return await self._error("point at the table (or any cell of it) for a list output")
            el = table
            d = await self.surface.describe(el)
            headers: list[str] = next(
                (c["strategy"]["headers"] for c in d["candidates"] if c["strategy"]["kind"] == "table"), []
            )
            columns = _map_columns(list(spec.columns or {}), headers)
            if columns is None:
                return await self._error(
                    f"cannot map output columns {list(spec.columns or {})} to table headers {headers}"
                )
            rows = await self.surface.read_table(el, columns)
            value: Any = [
                {
                    k: parse_value(v, (spec.columns or {}).get(k, "string"), spec.currency) if v else None
                    for k, v in r.items()
                }
                for r in rows
            ]
            action = "extract_table"
        else:
            d = await self.surface.describe(el)
            try:
                value = parse_value(await self.surface.read_text(el), spec.type, spec.currency)
            except (ValueError, ArithmeticError):
                return await self._error(
                    f"that element does not hold a {spec.type} value; pick the element with the value"
                )
            action = "extract"
        if not _output_strategies(d, self.spec.sample_inputs):
            return await self._error(
                "no stable way to find that element again; point at a value cell in a table with "
                "headers, or a value next to a label"
            )
        self.outputs[name] = value
        step = TraceStep(
            index=len(self.trace),
            actor="agent",
            action=action,
            container=container,
            describe=d,
            reason=self.redactor.scrub_text(a.get("reason", ""), for_model=True),
            output=name,
            columns=columns,
            before=await self._state(),
        )
        step.after = step.before
        self.trace.append(step)
        self.recorder.event("step_grounded", **step.summary())
        self.recorder.event(
            "output_extracted",
            output=name,
            output_type=spec.type,
            value=self.redactor.output_for_log(value, spec.sensitivity),
        )
        missing = [o for o in self.spec.outputs if o not in self.outputs]
        return ToolResult(
            [
                {
                    "type": "text",
                    "text": f"Recorded output {name}."
                    + (f" Still to record: {missing}." if missing else " All outputs recorded."),
                }
            ]
        )

    async def _tool_report_outcome(self, a: dict[str, Any]) -> ToolResult:
        assert self.snap is not None
        message = None
        try:
            _, el = await self.surface.element_for_ref(self.snap, a.get("ref", ""))
            message = self.redactor.scrub_text(await self.surface.read_text(el))
        except StaleRef:
            pass
        code = re.sub(r"[^A-Z0-9_]", "_", str(a.get("code", "OUTCOME")).upper())
        self._finish(
            "business_outcome",
            code=code,
            message=message or self.redactor.scrub_text(a.get("description", "")),
        )
        return ToolResult([{"type": "text", "text": "Outcome recorded. The run is over."}])

    async def _tool_request_human(self, a: dict[str, Any]) -> ToolResult:
        return await self._escalate_stuck(f"the agent asked for help: {a.get('reason', '')}")

    async def _tool_wait_for_change(self, a: dict[str, Any]) -> ToolResult:
        seconds = max(1, min(10, int(a.get("seconds", 2))))
        await asyncio.sleep(seconds)
        notes = await self._settle_and_guard(await self.session.red_baseline())
        blocks = await self.observe()
        return ToolResult(
            [{"type": "text", "text": "Waited." + (" " + " ".join(notes) if notes else "")}, *blocks]
        )

    async def _tool_finish(self, a: dict[str, Any]) -> ToolResult:
        assert self.snap is not None
        missing = [o for o in self.spec.outputs if o not in self.outputs]
        if missing:
            return await self._error(f"outputs not recorded yet: {missing}")
        frame, el = await self.surface.element_for_ref(self.snap, a.get("success_ref", ""))
        d = await self.surface.describe(el)
        state = await self._state()
        container = tuple(self.surface.container_of(frame))
        own = (await self.surface.read_text(el) or d.get("name") or "").strip()
        frame_state = state.frames.get(container)
        landmark = (
            own
            if own and own in (frame_state.emph if frame_state else [])
            else (frame_state.title if frame_state else "")
        )
        self._finish(
            "succeeded",
            summary=self.redactor.scrub_text(a.get("summary", ""), for_model=True),
            success={"container": list(container), "landmark": landmark, "state": state},
        )
        return ToolResult([{"type": "text", "text": "Finished."}])

    # ------------------------------------------------------------------ humans & stuck
    def _record(self, handoff: Handoff) -> None:
        req = handoff.request
        self.interventions.append(
            InterventionRecord(
                id=req.id,
                kind=req.kind,
                reason=self.redactor.scrub_text(req.reason),
                step_id=None,
                decision=req.decision,
                operator=req.operator,
                note=req.note,
                requested_at=req.requested_at,
                resolved_at=req.resolved_at,
                human_actions=handoff.human_actions,
            )
        )

    async def _after_handoff(self, handoff: Handoff) -> ToolResult:
        if handoff.kind in ("parked", "timeout"):
            self._finish("needs_human", code="NEEDS_HUMAN", message=handoff.request.reason)
            return ToolResult([{"type": "text", "text": "Waiting for a human; the run is parked."}], True)
        if handoff.kind == "abort":
            self._finish(
                "failed", code="ABORTED_BY_OPERATOR", message=f"operator {handoff.request.operator} aborted"
            )
            return ToolResult([{"type": "text", "text": "The operator aborted the run."}], True)
        state = await self._state()
        for cap in handoff.captured:
            step = _human_step(len(self.trace), cap, self.spec.sample_inputs)
            step.effect = self.policy.classify(step.action, step.describe.get("control"))
            if step.effect == "commit":
                self.committed = True
            self.trace.append(step)
        if self.trace and self.trace[-1].actor == "human":
            self.trace[-1].after = state
        summary = "; ".join(
            f"{h.type} {h.target}" + (f" = {h.value}" if h.value else "") for h in handoff.human_actions
        )
        note = (
            f"A human operator ({handoff.request.operator}) had control and handed it back. "
            f"They did: {summary or 'nothing recorded'}. Note: {handoff.request.note or '-'}. "
            "Continue from the current state."
        )
        blocks = await self.observe()
        return ToolResult([{"type": "text", "text": "Control is back with you."}, *blocks], note=note)

    async def _escalate_stuck(self, reason: str) -> ToolResult:
        if not self.controller.operator_available:
            self._finish("failed", code="DISCOVERY_STUCK", message=reason)
            return ToolResult(
                [{"type": "text", "text": "No operator is available; the run stops here."}], True
            )
        handoff = await self.controller.escalate(
            kind="stuck",
            reason=reason,
            allowed=["take_control", "abort"],
            goal=self.spec.goal,
            screenshot=await self.session.screenshot("stuck"),
            snapshot_excerpt=await self.session.excerpt(),
        )
        self._record(handoff)
        self.consecutive_errors = self.no_progress = self.policy_denials = 0
        return await self._after_handoff(handoff)

    async def _stuck(self, reason: str, terminal: bool = False) -> None:
        self.recorder.event("stuck_detected", reason=reason)
        if terminal or not self.controller.operator_available:
            self._finish("failed", code="DISCOVERY_STUCK", message=reason)
            return
        result = await self._escalate_stuck(reason)
        if self.final is None:
            self.messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Update after operator help:"}, *result.blocks],
                }
            )
            if result.note:
                self._note(result.note)

    async def _check_stuck(self) -> None:
        if self.consecutive_errors >= 3:
            await self._stuck("three consecutive failed actions")
        elif self.policy_denials >= 2:
            await self._stuck("the agent keeps attempting actions the policy does not allow")
        elif self.no_progress >= 4:
            await self._stuck("four actions without any change on the page")
        elif len(self.last_actions) >= 3 and len(set(self.last_actions[-3:])) == 1:
            await self._stuck("the same action was repeated three times")

    def _track_progress(self, action: str, d: dict[str, Any], before: PageState, after: PageState) -> None:
        fp = f"{action}:{d.get('role')}:{d.get('name') or d.get('label')}"
        self.last_actions.append((fp, str(before.key())))
        if action in ("click", "press_key") and not after.changed_containers(before):
            self.no_progress += 1
        else:
            self.no_progress = 0


def _map_columns(wanted: list[str], headers: list[str]) -> dict[str, str] | None:
    """Map output column names (share_id) onto table headers ("Share ID")."""
    out: dict[str, str] = {}
    by_snake = {_snake(h): h for h in headers}
    for col in wanted:
        if col in by_snake:
            out[col] = by_snake[col]
            continue
        tokens = set(col.split("_"))
        best = max(headers, key=lambda h: len(tokens & set(_snake(h).split("_"))), default=None)
        if best is None or not tokens & set(_snake(best).split("_")):
            return None
        out[col] = best
    return out


def _output_strategies(d: dict[str, Any], samples: dict[str, str]) -> list[dict[str, Any]]:
    """Strategies that can find an output again for a *different* record: never its own value."""
    keep = []
    sample_values = {_norm(v) for v in samples.values() if len(v) >= 3}
    for c in d.get("candidates", []):
        s = c["strategy"]
        if not c.get("unique") or s["kind"] not in ("label", "table_cell", "table"):
            continue
        if s["kind"] == "table_cell" and _norm(str(s["row"]["equals"])) in sample_values:
            keep.append(s)  # compiler parameterizes it
            continue
        keep.append(s)
    return keep


def _human_step(index: int, captured: dict[str, Any], samples: dict[str, str]) -> TraceStep:
    payload = captured["payload"]
    d = payload.get("describe") or {}
    kind = payload.get("type")
    role = d.get("role")
    action = {"click": "click", "enter": "press_key"}.get(
        kind, "select" if role in ("combobox", "listbox") else "fill"
    )
    return TraceStep(
        index=index,
        actor="human",
        action=action,
        container=list(captured["container"]),
        describe=d,
        reason="performed by a human operator",
        effect="read" if action == "click" else "input",
        value_text=captured.get("value"),
        key="Enter" if action == "press_key" else None,
    )


__all__ = ["AgentOutcome", "DiscoveryAgent", "money_variants"]
