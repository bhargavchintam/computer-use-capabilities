"""Discovery end to end: goal -> typed spec -> LLM-driven run -> trace -> compiled
artifact -> lint -> verify-by-replay (fresh session, different inputs) -> registry (draft)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agent.llm import DEFAULT_MODEL, FALLBACK_BETA, AnthropicLLM, LLMClient, anthropic_client
from .agent.loop import DiscoveryAgent
from .agent.prompts import GOAL_COMPILER
from .compiler import CompileError, compile_capability, lint
from .configio import (
    dump_yaml,
    load_app_profile,
    load_policy,
    load_tenant,
    model_to_yaml,
    repo_root,
    shown_path,
)
from .control import SessionController
from .evidence import RunRecorder
from .models import AppProfile, Capability, GoalSpec, GoalSpecDraft
from .models.results import InterventionRecord, RunResult
from .policy import PolicyEngine
from .redaction import Redactor
from .registry import Registry
from .replay import OperatorHook, OperatorMode, run_replay
from .runtime import RuntimeSession


class DiscoveryReport(BaseModel):
    run_id: str
    status: str
    code: str | None = None
    message: str | None = None
    goal: str | None = None
    capability: str | None = None
    capability_path: str | None = None
    verification: dict[str, Any] | None = None
    compile_notes: list[str] = Field(default_factory=list)
    lint_findings: list[str] = Field(default_factory=list)
    turns: int = 0
    models: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    evidence_dir: str | None = None


async def compile_goal(
    goal: str, app: AppProfile, model: str | None = None
) -> tuple[GoalSpec, dict[str, Any]]:
    """One structured-output call: natural-language goal -> typed, reviewable GoalSpec."""
    model = model or os.environ.get("CUA_MODEL", DEFAULT_MODEL)
    client = anthropic_client()
    extra: dict[str, Any] = {}
    if model.startswith(("claude-opus-5", "claude-fable")):
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    resp = await client.beta.messages.parse(
        model=model,
        max_tokens=8000,
        messages=[{"role": "user", "content": GOAL_COMPILER.format(app=app.description, goal=goal)}],
        output_format=GoalSpecDraft,
        **extra,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"goal compilation failed (stop_reason={resp.stop_reason})")
    meta = {"message_id": resp.id, "model": resp.model, "usage": resp.usage.model_dump(exclude_none=True)}
    return resp.parsed_output.to_spec(), meta


def _next_version(registry: Registry, cap: Capability) -> str:
    existing = registry.versions(cap.id)
    if not existing:
        return cap.version
    latest = existing[-1]
    major, minor, _ = (int(x) for x in latest.version.split("."))
    same_contract = latest.contract.model_dump() == cap.contract.model_dump()
    return f"{major}.{minor + 1}.0" if same_contract else f"{major + 1}.0.0"


def _drop_landmark(cap: Capability, step_id: str | None) -> Capability | None:
    """Calibration: a landmark that did not hold for a second record was data, not UI. Drop it."""
    data = cap.model_dump(mode="json", exclude_none=True)
    if step_id is None:
        conds = data["implementation"]["success"]["all_of"]
        kept = [c for c in conds if not ("text_visible" in c and isinstance(c["text_visible"]["text"], str))]
        if len(kept) == len(conds):
            return None
        data["implementation"]["success"]["all_of"] = kept
    else:
        step = next((s for s in data["implementation"]["steps"] if s["id"] == step_id), None)
        if step is None:
            return None
        kept = [c for c in step.get("expect", []) if condition_kind_dict(c) != "text_visible"]
        if len(kept) == len(step.get("expect", [])):
            return None
        step["expect"] = kept
    data["provenance"].setdefault("notes", []).append(
        f"calibration: dropped a landmark checkpoint on {step_id or 'success'} that did not hold for a second record"
    )
    return Capability.model_validate(data)


def condition_kind_dict(c: dict[str, Any]) -> str:
    return next(iter(c))


async def run_discovery(
    *,
    tenant_id: str,
    goal: str | None = None,
    spec: GoalSpec | None = None,
    verify_inputs: dict[str, str] | None = None,
    headed: bool | None = None,
    operator: OperatorMode = "none",
    operator_hook: OperatorHook | None = None,
    llm: LLMClient | None = None,
    runs_root: Path | None = None,
    registry: Registry | None = None,
    max_turns: int = 40,
    vision: bool = True,
    console_port: int = 8765,
    record_video: bool = False,
) -> DiscoveryReport:
    tenant = load_tenant(tenant_id)
    app = load_app_profile(tenant.app)
    policy_cfg = load_policy(tenant.policy)
    runs_root = runs_root or repo_root() / "runs"
    registry = registry or Registry()
    redactor = Redactor(app.sensitive_labels)
    recorder = RunRecorder(runs_root, "discovery", redactor)
    report = DiscoveryReport(run_id=recorder.run_id, status="failed", evidence_dir=str(recorder.dir))

    if spec is None:
        if not goal:
            raise ValueError("give a natural-language goal or a goal spec")
        spec, meta = await compile_goal(goal, app)
        for name, v in spec.sample_inputs.items():
            redactor.add_param(name, v, spec.inputs[name].sensitivity)
        recorder.event("goal_compiled", goal_template=spec.goal, capability_id=spec.capability_id, **meta)
    else:
        for name, v in spec.sample_inputs.items():
            redactor.add_param(name, v, spec.inputs[name].sensitivity)
    report.goal = spec.goal
    persisted_spec = spec.model_dump(mode="json")
    persisted_spec["sample_inputs"] = {k: redactor.scrub_text(v) for k, v in spec.sample_inputs.items()}
    (recorder.dir / "goal_spec.yaml").write_text(dump_yaml(persisted_spec), encoding="utf-8")
    recorder.event(
        "run_started",
        mode="discovery",
        tenant=tenant.id,
        environment=tenant.environment,
        goal_template=spec.goal,
        operator=operator,
    )

    policy = PolicyEngine(policy_cfg, tenant, app)
    controller = SessionController(
        recorder,
        redactor,
        operator_available=operator != "none",
        handoff_timeout_s=app.timeouts.handoff_ms / 1000,
    )
    session = RuntimeSession(
        tenant=tenant,
        app=app,
        policy=policy,
        redactor=redactor,
        recorder=recorder,
        controller=controller,
        headed=operator == "console" if headed is None else headed,  # a person needs a window to take over
        record_video=record_video,
    )
    console = None
    hook_task: asyncio.Task[None] | None = None
    llm = llm or AnthropicLLM()
    try:
        await session.start()
        if operator == "console":
            from .operator_console import OperatorConsole

            console = OperatorConsole(controller, recorder, port=console_port)
            await console.start()
        if operator_hook is not None:
            hook_task = asyncio.create_task(operator_hook(controller, session))
        await session.sign_on()
        await session.goto(app.home_route)
        agent = DiscoveryAgent(
            spec=spec,
            session=session,
            controller=controller,
            recorder=recorder,
            redactor=redactor,
            policy=policy,
            llm=llm,
            app=app,
            entry_route=app.home_route,
            max_turns=max_turns,
            vision=vision,
        )
        outcome = await agent.run()
    finally:
        if hook_task:
            hook_task.cancel()
        await session.close()
        controller.close()
        if console:
            await console.stop()

    recorder.save_json("trace.json", [t.summary() for t in outcome.trace])
    report.status, report.code, report.message = outcome.status, outcome.code, outcome.message
    report.turns, report.models, report.usage, report.interventions = (
        outcome.turns,
        outcome.models,
        outcome.usage,
        outcome.interventions,
    )

    if outcome.status == "succeeded":
        await _compile_and_verify(
            report,
            spec,
            outcome,
            app,
            tenant_id,
            tenant,
            recorder,
            redactor,
            registry,
            runs_root,
            verify_inputs,
        )
    persisted = report.model_dump(mode="json")
    for key in ("evidence_dir", "capability_path"):
        if persisted.get(key):
            persisted[key] = shown_path(persisted[key])
    if persisted.get("verification") and persisted["verification"].get("evidence_dir"):
        persisted["verification"]["evidence_dir"] = shown_path(persisted["verification"]["evidence_dir"])
    recorder.save_json("discovery_report.json", persisted)
    recorder.event("discovery_report", status=report.status, code=report.code, capability=report.capability)
    recorder.close()
    return report


async def _compile_and_verify(
    report: DiscoveryReport,
    spec: GoalSpec,
    outcome: Any,
    app: AppProfile,
    tenant_id: str,
    tenant: Any,
    recorder: RunRecorder,
    redactor: Redactor,
    registry: Registry,
    runs_root: Path,
    verify_inputs: dict[str, str] | None,
) -> None:
    try:
        cap, notes = compile_capability(
            spec=spec, outcome=outcome, app=app, tenant=tenant, run_id=recorder.run_id
        )
    except CompileError as e:
        report.status, report.code, report.message = "failed", "COMPILE_FAILED", str(e)
        recorder.event("compile_failed", error=str(e))
        return
    cap = cap.model_copy(update={"version": _next_version(registry, cap)})
    report.compile_notes = notes
    findings = lint(cap, samples=spec.sample_inputs, secrets=redactor.secret_values)
    report.lint_findings = findings
    if findings:  # fail closed: a leaky artifact is never written anywhere
        report.status, report.code, report.message = "failed", "LINT_FAILED", "; ".join(findings)
        recorder.event("lint_failed", findings=findings)
        return
    recorder.event(
        "capability_compiled", capability=cap.ref, steps=len(cap.implementation.steps), notes=notes
    )
    inputs = {**spec.sample_inputs, **(verify_inputs or {})}
    approve = (
        outcome.commit_approved_by is not None
    )  # the operator approved this flow's commit in sandbox discovery
    verification: RunResult | None = None
    for attempt in range(3):
        (recorder.dir / "capability.candidate.yaml").write_text(model_to_yaml(cap), encoding="utf-8")
        verification = await run_replay(
            cap, tenant_id=tenant_id, inputs=inputs, approve=approve, runs_root=runs_root
        )
        recorder.event(
            "verify_by_replay",
            attempt=attempt + 1,
            verification_run=verification.run_id,
            status=verification.status,
            code=verification.error.code if verification.error else None,
        )
        if verification.status == "succeeded" or verification.error is None:
            break
        if verification.error.code not in ("CHECKPOINT_FAILED", "UNRECOGNIZED_STATE", "TIMEOUT"):
            break
        calibrated = _drop_landmark(cap, verification.error.step_id)
        if calibrated is None:
            break
        cap = calibrated
    assert verification is not None
    report.verification = {
        "run_id": verification.run_id,
        "status": verification.status,
        "error": verification.error.model_dump(mode="json") if verification.error else None,
        "evidence_dir": verification.evidence_dir,
    }
    if verification.status != "succeeded":
        report.status, report.code = "failed", "VERIFY_FAILED"
        report.message = (
            "the compiled artifact did not replay deterministically; kept in the run folder for review"
        )
        return
    cap.provenance.verified_by_runs.append(verification.run_id)
    path = registry.save(cap, overwrite=False)
    (recorder.dir / "capability.yaml").write_text(model_to_yaml(cap), encoding="utf-8")
    report.capability, report.capability_path = cap.ref, str(path)
    recorder.event(
        "capability_saved", capability=cap.ref, path=shown_path(path), content_sha256=cap.content_sha256()
    )


__all__ = ["DiscoveryReport", "compile_goal", "run_discovery"]
