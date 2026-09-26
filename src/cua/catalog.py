"""Agent-facing capability catalog.

Approved, version-compatible capabilities become typed tools generated from
their *contract* (inputs, outputs, business outcomes). A calling agent picks a
capability by name; executing it is deterministic replay. The model sits in the
calling agent, never in the execution path. Commit capabilities are exposed but
cannot be self-approved by the agent: without a human approval they park as
needs_human.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.version import Version

from .configio import load_app_profile, load_tenant, repo_root
from .evidence import RunRecorder
from .models import Capability, RunResult
from .redaction import Redactor
from .registry import Registry
from .tenancy import version_in

if TYPE_CHECKING:
    from .agent.llm import LLMClient

SYSTEM = """\
You are an assistant for staff at a credit union. You answer questions by calling the provided \
tools, which operate the core banking system deterministically and return structured results. \
Never guess values. If a tool returns a business outcome, explain it plainly using its guidance. \
If a tool says a human approval is required, say so; do not try to work around it."""


def tool_name(cap_id: str) -> str:
    return cap_id.replace(".", "__")[:64]


def eligible(tenant_id: str, registry: Registry | None = None) -> list[Capability]:
    tenant = load_tenant(tenant_id)
    latest: dict[str, Capability] = {}
    for cap in (registry or Registry()).all():
        if not cap.approval_valid():
            continue
        app = cap.implementation.app
        if app.product != tenant.app or not version_in(app.versions, tenant.product_version):
            continue
        if cap.id not in latest or Version(cap.version) > Version(latest[cap.id].version):
            latest[cap.id] = cap
    return list(latest.values())


def tool_for(cap: Capability) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, spec in cap.contract.inputs.items():
        desc = spec.description + (f" Format: {spec.pattern}." if spec.pattern else "")
        prop: dict[str, Any] = {"type": "string", "description": desc}
        if spec.enum:
            prop["enum"] = spec.enum
        props[name] = prop
    outputs = ", ".join(f"{k} ({v.type})" for k, v in cap.contract.outputs.items())
    outcomes = " ".join(
        f"{code}: {o.description} Guidance: {o.caller_guidance}" for code, o in cap.contract.outcomes.items()
    )
    effect = (
        "It COMMITS a change and needs a human approval."
        if cap.contract.effects == "commit"
        else "Read-only and safe to retry."
    )
    return {
        "name": tool_name(cap.id),
        "description": f"{cap.title}. {effect} Returns: {outputs}. Business outcomes: {outcomes}",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": [n for n, s in cap.contract.inputs.items() if s.required],
            "additionalProperties": False,
        },
    }


def caller_view(r: RunResult) -> dict[str, Any]:
    """What the calling agent gets back: the result contract, without evidence plumbing."""
    return {
        "status": r.status,
        "outputs": r.outputs,
        "outcome": r.outcome.model_dump(exclude_none=True) if r.outcome else None,
        "error": {"code": r.error.code, "message": r.error.message, "transient": r.error.transient}
        if r.error
        else None,
        "side_effect": r.side_effect,
        "retry_safe": r.retry_safe,
        "warnings": [w.code for w in r.warnings],
        "run_id": r.run_id,
    }


async def ask(
    question: str,
    tenant_id: str,
    *,
    registry: Registry | None = None,
    runs_root: Path | None = None,
    max_turns: int = 6,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    from .agent.llm import AnthropicLLM
    from .replay import run_replay

    runs_root = runs_root or repo_root() / "runs"
    caps = {tool_name(c.id): c for c in eligible(tenant_id, registry)}
    app = load_app_profile(load_tenant(tenant_id).app)
    redactor = Redactor(app.sensitive_labels)
    recorder = RunRecorder(runs_root, "catalog", redactor)
    llm = llm or AnthropicLLM(effort="medium")
    tools = [tool_for(c) for c in caps.values()]
    recorder.save_json("catalog.json", tools)
    recorder.event("catalog_loaded", tenant=tenant_id, tools=list(caps))
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    calls: list[dict[str, Any]] = []
    answer = ""
    for _ in range(max_turns):
        turn = await llm.step(system=SYSTEM, tools=tools, messages=messages)
        recorder.event("llm_turn", message_id=turn.id, model=turn.model, stop_reason=turn.stop_reason)
        messages.append({"role": "assistant", "content": turn.content})
        uses = [b for b in turn.content if getattr(b, "type", None) == "tool_use"]
        if not uses:
            answer = " ".join(
                getattr(b, "text", "") for b in turn.content if getattr(b, "type", None) == "text"
            ).strip()
            break
        results = []
        for use in uses:
            cap = caps.get(use.name)
            if cap is None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "is_error": True,
                        "content": "unknown tool",
                    }
                )
                continue
            inputs = {k: str(v) for k, v in dict(use.input).items()}
            for k, v in inputs.items():
                if k in cap.contract.inputs:
                    redactor.add_param(k, v, cap.contract.inputs[k].sensitivity)
            recorder.event(
                "capability_invoked",
                tool=use.name,
                capability=cap.ref,
                inputs={k: redactor.scrub_text(v) for k, v in inputs.items()},
            )
            result = await run_replay(
                cap, tenant_id=tenant_id, inputs=inputs, approve=False, runs_root=runs_root
            )
            view = caller_view(result)
            calls.append(
                {
                    "tool": use.name,
                    "capability": cap.ref,
                    "replay_run": result.run_id,
                    "status": result.status,
                }
            )
            recorder.event(
                "capability_result",
                tool=use.name,
                replay_run=result.run_id,
                status=result.status,
                outputs={
                    k: redactor.output_for_log(v, cap.contract.outputs[k].sensitivity)
                    for k, v in (result.outputs or {}).items()
                },
            )
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": json.dumps(view)})
        messages.append({"role": "user", "content": results})
    recorder.event("answer", text=redactor.scrub_text(answer))
    recorder.save_json("calls.json", calls)
    recorder.close()
    return {"answer": answer, "calls": calls, "evidence_dir": str(recorder.dir)}
