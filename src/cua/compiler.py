"""Compile a grounded discovery trace into a capability artifact.

What the compiler guarantees (and the linter enforces, failing closed):
* every target strategy was validated at record time to hit exactly the element acted on;
* outputs are located by label or column header + row key, never by their own value;
* concrete input values never appear: they become {param} references;
* no secret, PII-shaped string or money value appears anywhere in the artifact;
* every navigating step has a postcondition (new document + a safe landmark);
* every commit step has pre-checks that the review page echoes the inputs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .agent.loop import AgentOutcome
from .checks import money_variants
from .models import (
    AppProfile,
    Capability,
    GoalSpec,
    LiteralValue,
    OutcomeSpec,
    ParamRef,
    Provenance,
    TenantConfig,
)
from .models.capability import AppBinding, CallExample, Contract, Entry, Implementation, OutcomeDetector, Step
from .models.conditions import (
    AllOf,
    DocumentChanged,
    FieldValue,
    FieldValueArgs,
    OutputsPresent,
    TextArgs,
    TextVisible,
)
from .models.targets import Fingerprint, Target
from .redaction import API_KEY, CARD, EMAIL, MONEY, PHONE, SSN, _luhn
from .trace import FrameState, PageState, TraceStep


class CompileError(Exception):
    pass


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _slug(s: str) -> str:
    s = s.replace("#", " number ")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "step"


# ---------------------------------------------------------------------------------- pruning


def prune(steps: list[TraceStep]) -> tuple[list[TraceStep], list[str]]:
    """Drop superseded steps, detours that looped back to an earlier screen, and navigation
    that was immediately overwritten without being used. Verify-by-replay proves the result."""
    notes: list[str] = []
    kept = [s for s in steps if not s.superseded]
    if len(kept) != len(steps):
        notes.append(f"dropped {len(steps) - len(kept)} step(s) made irrelevant by a session restart")
    # 1) loops: the page returns to an earlier screen with nothing committed/entered/extracted in between
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(kept):
            if s.before is None:
                continue
            for j in range(len(kept) - 1, i - 1, -1):
                t = kept[j]
                if t.after is None or t.after.key() != s.before.key():
                    continue
                span = kept[i : j + 1]
                if any(
                    x.effect == "commit" or x.action in ("fill", "select", "extract", "extract_table")
                    for x in span
                ):
                    continue
                notes.append(f"removed a detour of {len(span)} step(s) that returned to the same screen")
                kept = kept[:i] + kept[j + 1 :]
                changed = True
                break
            if changed:
                break
    # 2) a navigation whose result was replaced before anything happened on it
    out: list[TraceStep] = []
    for idx, s in enumerate(kept):
        nxt = kept[idx + 1] if idx + 1 < len(kept) else None
        if (
            s.action == "click"
            and s.effect == "read"
            and s.before
            and s.after
            and nxt
            and nxt.action == "click"
            and nxt.before
            and nxt.after
            and nxt.container != _changed_one(s)
        ):
            mine = _changed_one(s)
            if mine and mine in [list(c) for c in nxt.after.changed_containers(nxt.before)]:
                notes.append("removed a navigation that was immediately replaced without being used")
                continue
        out.append(s)
    return out, notes


def _changed_one(s: TraceStep) -> list[str] | None:
    if s.before is None or s.after is None:
        return None
    changed = s.after.changed_containers(s.before)
    return list(changed[0]) if len(changed) == 1 else None


# ---------------------------------------------------------------------------------- targets


def _parameterize(strategy: dict[str, Any], samples: dict[str, str]) -> dict[str, Any] | None:
    """Swap a concrete input value inside a strategy for a {param}; drop strategies that embed data."""
    values = {name: v for name, v in samples.items() if len(v) >= 3}
    if strategy["kind"] == "table_cell":
        for name, v in values.items():
            if _norm(strategy["row"]["equals"]) == _norm(v):
                return {**strategy, "row": {**strategy["row"], "equals": {"param": name}}}
    blob = json.dumps(strategy).lower()
    if any(v.lower() in blob for v in values.values()):
        return None
    return strategy


def _target(step: TraceStep, samples: dict[str, str]) -> Target:
    d = step.describe or {}
    extraction = step.action in ("extract", "extract_table")
    allowed = ("label", "table_cell", "table") if extraction else ("role_name", "label", "attr")
    strategies: list[dict[str, Any]] = []
    for c in d.get("candidates", []):
        s = c.get("strategy") or {}
        if not c.get("unique") or s.get("kind") not in allowed:
            continue
        s = _parameterize(s, samples)
        if s is not None and s not in strategies:
            strategies.append(s)
    # prefer row keys that are vocabulary ("PRIMARY SAVINGS") over codes ("S01")
    strategies.sort(
        key=lambda s: (s["kind"] == "table_cell" and bool(re.search(r"\d", str(s["row"]["equals"]))),)
    )
    order = {"role_name": 0, "label": 1, "table_cell": 2, "table": 3, "attr": 4}
    strategies.sort(key=lambda s: order[s["kind"]])
    if not strategies:
        raise CompileError(
            f"trace step {step.index} ({step.action} {d.get('role')} {d.get('name')!r}): "
            "no validated, data-free locator"
        )
    fp = d.get("fingerprint") or {}
    return Target.model_validate(
        {
            "container": step.container,
            "strategies": strategies,
            "fingerprint": Fingerprint(
                tag=fp.get("tag") or d.get("tag") or "unknown",
                role=fp.get("role"),
                name=None if extraction or fp.get("role") == "cell" else fp.get("name"),
                label=fp.get("label") or None,
                css_path=fp.get("css_path"),
                bbox=[float(x) for x in fp["bbox"]] if fp.get("bbox") else None,
            ).model_dump(exclude_none=True),
        }
    )


# ---------------------------------------------------------------------------------- checkpoints


_SAFE_LANDMARK = re.compile(r"^[A-Za-z][A-Za-z ,'&/()-]{2,39}$")


def _landmark(before: FrameState | None, after: FrameState, avoid: list[str]) -> str | None:
    seen = (
        set(before.emph)
        if before and before.doc_id != after.doc_id and before.url_path == after.url_path
        else set()
    )
    for text in after.emph:
        t = text.strip()
        if t in seen or not _SAFE_LANDMARK.match(t):
            continue
        if any(a and _norm(a) in _norm(t) for a in avoid):
            continue
        return t
    return None


def _expect(step: TraceStep, avoid: list[str], value: Any) -> list[Any]:
    if step.action == "fill" and isinstance(value, ParamRef):
        return [FieldValue(field_value=FieldValueArgs(equals=value))]
    if step.action not in ("click", "press_key") or step.before is None or step.after is None:
        return []
    out: list[Any] = []
    for c in step.after.changed_containers(step.before):
        if not c:
            continue  # top-level document changes (sign-on/off) are not flow checkpoints
        out.append(DocumentChanged(document_changed={"container": list(c)}))
        mark = _landmark(step.before.frames.get(c), step.after.frames[c], avoid)
        if mark:
            out.append(TextVisible(text_visible=TextArgs(container=list(c), text=mark)))
    return out


def _visible(value: str, text: str, money: bool) -> bool:
    hay = _norm(text)
    variants = money_variants(value) if money else [value]
    return any(_norm(v) in hay for v in variants)


def _pre_checks(step: TraceStep, spec: GoalSpec) -> list[Any]:
    """Commit only after the review page shows every input we are about to commit."""
    if step.effect != "commit" or step.before is None:
        return []
    frame = step.before.frames.get(tuple(step.container))
    if frame is None:
        return []
    out = []
    for name, value in spec.sample_inputs.items():
        if _visible(value, frame.text, spec.inputs[name].type == "money"):
            out.append(
                TextVisible(text_visible=TextArgs(container=step.container, text=ParamRef(param=name)))
            )
    return out


# ---------------------------------------------------------------------------------- compile


def compile_capability(
    *,
    spec: GoalSpec,
    outcome: AgentOutcome,
    app: AppProfile,
    tenant: TenantConfig,
    run_id: str,
    version: str = "1.0.0",
) -> tuple[Capability, list[str]]:
    if outcome.status != "succeeded" or not outcome.success:
        raise CompileError("only a successful discovery run can be compiled")
    samples = spec.sample_inputs
    output_texts = [json.dumps(v) for v in outcome.outputs.values()]
    avoid = [*samples.values(), *output_texts]
    trace, notes = prune(outcome.trace)
    steps: list[Step] = []
    used: set[str] = set()
    for t in trace:
        target = _target(t, samples)
        value: Any = None
        if t.action in ("fill", "select"):
            text = t.value_text or ""
            m = re.fullmatch(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", text.strip())
            if m:
                value = ParamRef(param=m.group(1))
            else:
                match = next((n for n, v in samples.items() if _norm(v) == _norm(text)), None)
                value = ParamRef(param=match) if match else LiteralValue(literal=text)
        d = t.describe or {}
        subject = t.output or d.get("label") or d.get("name") or d.get("tag") or "element"
        verb = {"extract": "extract", "extract_table": "extract", "press_key": "press"}.get(
            t.action, t.action
        )
        base_id = f"{verb}_{_slug(subject)}"
        step_id, n = base_id, 2
        while step_id in used:
            step_id, n = f"{base_id}_{n}", n + 1
        used.add(step_id)
        intent = (t.reason or f"{t.action} {subject}")[:120]
        steps.append(
            Step.model_validate(
                {
                    "id": step_id,
                    "intent": intent,
                    "action": t.action,
                    "effect": "read" if t.action in ("extract", "extract_table") else t.effect,
                    "target": target,
                    "value": value,
                    "key": t.key if t.action == "press_key" else None,
                    "output": t.output,
                    "parse": spec.outputs[t.output].type if t.action == "extract" and t.output else None,
                    "columns": t.columns,
                    "pre": _pre_checks(t, spec),
                    "expect": _expect(t, avoid, value),
                    "provenance": t.actor,
                }
            )
        )
        if t.actor == "human":
            notes.append(f"step {step_id} was performed by a human operator: review before approval")

    # success: landmark + identity checkpoint + outputs
    success = outcome.success
    container = success["container"]
    final: PageState = success["state"]
    conds: list[Any] = []
    mark = success.get("landmark")
    if mark and _SAFE_LANDMARK.match(mark) and not any(_norm(a) in _norm(mark) for a in avoid if a):
        conds.append(TextVisible(text_visible=TextArgs(container=container, text=mark)))
    frame = final.frames.get(tuple(container))
    for name, v in samples.items():
        if spec.inputs[name].sensitivity == "pii_identifier" and frame and _visible(v, frame.text, False):
            conds.append(TextVisible(text_visible=TextArgs(container=container, text=ParamRef(param=name))))
    conds.append(OutputsPresent(outputs_present=list(spec.outputs)))

    # business outcomes that can appear on the screens this flow visits
    visited = {
        f.url_path for t in outcome.trace for st in (t.before, t.after) if st for f in st.frames.values()
    }
    outcomes: dict[str, OutcomeSpec] = {}
    detectors: dict[str, OutcomeDetector] = {}
    for msg_id, m in app.messages.items():
        if m.routes and not any(fnmatch.fnmatch(p, r) for p in visited for r in m.routes):
            continue
        outcomes[m.outcome] = OutcomeSpec(
            description=m.description, caller_guidance=m.caller_guidance, retry_safe=m.retry_safe
        )
        detectors[m.outcome] = OutcomeDetector(ref=msg_id)

    commits = any(s.effect == "commit" for s in steps)
    major, minor = tenant.product_version.split(".")[:2]
    cap_id = (
        spec.capability_id
        if spec.capability_id.startswith(app.product + ".")
        else f"{app.product}.{spec.capability_id}"
    )
    flow = " -> ".join(s.intent.rstrip(".") for s in steps)
    trace_blob = json.dumps([t.summary() for t in outcome.trace], default=str, sort_keys=True)
    cap = Capability(
        id=cap_id,
        version=version,
        status="draft",
        title=spec.title,
        description=f"Discovered for: {spec.goal}\nFlow: {flow}",
        contract=Contract(
            inputs=spec.inputs,
            outputs=spec.outputs,
            outcomes=outcomes,
            effects="commit" if commits else "read_only",
            idempotent=not commits,
            example=CallExample(
                input={k: f"<{v.type}>" for k, v in spec.inputs.items()},
                output={k: _shape(v.type, v.columns) for k, v in spec.outputs.items()},
            ),
        ),
        implementation=Implementation(
            app=AppBinding(product=app.product, versions=f">={major}.{minor},<{int(major) + 1}.0"),
            entry=Entry(route=app.home_route),
            steps=steps,
            outcome_detectors=detectors,
            success=AllOf(all_of=conds),
        ),
        provenance=Provenance(
            source="discovery",
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            goal=spec.goal,
            run_id=run_id,
            model=", ".join(outcome.models) or None,
            tenant=tenant.id,
            environment=tenant.environment,
            trace_sha256=hashlib.sha256(trace_blob.encode()).hexdigest(),
            notes=notes,
        ),
    )
    return cap, notes


def _shape(kind: str, columns: dict[str, str] | None) -> Any:
    if kind == "money":
        return {"amount": "<decimal>", "currency": "USD"}
    if kind == "table":
        return [{c: _shape(t, None) for c, t in (columns or {}).items()}]
    return f"<{kind}>"


# ---------------------------------------------------------------------------------- linter


def lint(cap: Capability, *, samples: dict[str, str], secrets: list[str]) -> list[str]:
    """Fail closed: nothing concrete, secret or PII-shaped may be persisted in an artifact."""
    data = cap.model_dump(mode="json", exclude_none=True)
    for spec in data["contract"]["inputs"].values():  # enum options are UI vocabulary, not data
        spec.pop("enum", None)
    blob = json.dumps(data, ensure_ascii=False)
    findings: list[str] = []
    for s in secrets:
        if s and s.lower() in blob.lower():
            findings.append("a secret value appears in the artifact")
    for name, v in samples.items():
        if len(v) >= 3 and re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", blob, re.I):
            findings.append(f"the concrete value of input {name!r} appears (it must be a {{param}})")
    for label, rx in (("SSN", SSN), ("email", EMAIL), ("phone", PHONE), ("API key", API_KEY)):
        if rx.search(blob):
            findings.append(f"a {label}-shaped string appears")
    if any(_luhn(re.sub(r"\D", "", m.group())) for m in CARD.finditer(blob)):
        findings.append("a card-number-shaped string appears")
    impl = json.dumps(data["implementation"])
    if MONEY.search(impl):
        findings.append("a money value appears in the implementation (data leaked into a target/checkpoint)")
    return findings
