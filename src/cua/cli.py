"""`cua`: discover capabilities with an LLM, replay them deterministically, manage the registry."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .configio import load_model, load_tenant, model_to_yaml, repo_root
from .models import Capability, GoalSpec, RunResult
from .registry import NotFound, Registry
from .replay import OperatorMode

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
mock_app = typer.Typer(
    no_args_is_help=True, help="Run and poke the local AcmeCore mock bank (synthetic data)."
)
caps_app = typer.Typer(no_args_is_help=True, help="Inspect, validate and approve capability artifacts.")
catalog_app = typer.Typer(
    no_args_is_help=True, help="Approved capabilities as typed tools for a calling agent."
)
app.add_typer(mock_app, name="mock")
app.add_typer(caps_app, name="capabilities")
app.add_typer(catalog_app, name="catalog")
console = Console()


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"expected name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def _operator(value: str) -> OperatorMode:
    if value not in ("none", "console"):
        raise typer.BadParameter("--operator must be none or console")
    return value  # type: ignore[return-value]


def _load_cap(ref: str) -> Capability:
    path = Path(ref)
    if path.suffix in (".yaml", ".yml") and path.exists():
        return load_model(path, Capability)
    try:
        return Registry().get(ref)
    except NotFound as e:
        raise typer.BadParameter(str(e)) from e


# ---------------------------------------------------------------------------------- replay


def print_result(r: RunResult) -> None:
    colour = {
        "succeeded": "green",
        "business_outcome": "yellow",
        "rejected": "magenta",
        "needs_human": "cyan",
        "failed": "red",
    }[r.status]
    console.print(
        f"\n[bold {colour}]{r.status.upper()}[/]  {r.capability}  tenant={r.tenant}  "
        f"side_effect={r.side_effect}  retry_safe={r.retry_safe}  ({r.duration_ms} ms)"
    )
    if r.outputs:
        console.print("[bold]outputs[/]", json.dumps(r.outputs, indent=2))
    if r.outcome:
        console.print(
            f"[bold]outcome[/] {r.outcome.code}: {r.outcome.message}\n  guidance: {r.outcome.caller_guidance}"
        )
    if r.error:
        e = r.error
        console.print(f"[bold]error[/] {e.code} at step {e.step_id}: {e.message}")
        for label, value in (("expected", e.expected), ("observed", e.observed), ("hint", e.hint)):
            if value:
                console.print(f"  {label}: {value}")
    if r.parked:
        console.print(
            f"[bold]parked[/] intervention {r.parked.intervention_id} (deadline {r.parked.deadline})"
        )
    for rec in r.recoveries:
        console.print(f"  recovered: {rec.detector} -> {rec.action}")
    for w in r.warnings:
        console.print(f"  [yellow]warning[/] {w.code} ({w.step_id}): {w.detail}")
    for iv in r.interventions:
        console.print(
            f"  intervention {iv.id} [{iv.kind}] decision={iv.decision} by {iv.operator}: "
            f"{len(iv.human_actions)} human action(s)"
        )
    t = Table(show_header=True, header_style="bold", box=None)
    for col in ("step", "status", "strategy", "attempts", "ms"):
        t.add_column(col)
    for s in r.steps:
        t.add_row(s.step_id, s.status, s.strategy or "", str(s.attempts), str(s.duration_ms))
    if r.steps:
        console.print(t)
    console.print(
        f"[dim]evidence: {r.evidence_dir}  plan: {r.effective_plan_sha256 and r.effective_plan_sha256[:16]}  "
        f"overlays: {r.overlays_applied or '-'}[/]"
    )


@app.command()
def replay(
    capability: Annotated[
        str, typer.Argument(help="capability id[@version] from the registry, or a YAML path")
    ],
    tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")],
    input: Annotated[list[str], typer.Option("--input", "-i", help="name=value (repeatable)")] = [],  # noqa: B006
    approve: Annotated[bool, typer.Option(help="invocation-level approval for commit steps")] = False,
    headed: bool = False,
    operator: Annotated[str, typer.Option(help="none | console")] = "none",
    no_overlays: Annotated[bool, typer.Option(help="ignore version overlays (drift demo)")] = False,
    video: bool = False,
    as_json: Annotated[bool, typer.Option("--json", help="print the full result as JSON")] = False,
) -> None:
    """Replay a capability deterministically (no model) and print the structured result."""
    from .replay import run_replay

    cap = _load_cap(capability)
    result = asyncio.run(
        run_replay(
            cap,
            tenant_id=tenant,
            inputs=_kv(input),
            approve=approve,
            headed=headed or None,  # default: headed only when an operator may take over
            operator=_operator(operator),
            use_overlays=not no_overlays,
            record_video=video,
        )
    )
    if as_json:
        print(result.model_dump_json(indent=2))
    else:
        print_result(result)
    raise typer.Exit(0 if result.status in ("succeeded", "business_outcome") else 1)


# ---------------------------------------------------------------------------------- discover


@app.command()
def discover(
    tenant: Annotated[str, typer.Option(help="tenant id to discover on (should be a sandbox)")],
    goal: Annotated[
        str | None, typer.Option(help="natural-language goal, e.g. 'look up member 10042 ...'")
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="a reviewed goal spec YAML instead of --goal")] = None,
    verify_input: Annotated[list[str], typer.Option(help="name=value for the verify-by-replay run")] = [],  # noqa: B006
    headed: bool = False,
    operator: Annotated[str, typer.Option(help="none | console")] = "none",
    max_turns: int = 40,
    no_vision: Annotated[
        bool, typer.Option(help="text observations only (no screenshots to the model)")
    ] = False,
    video: bool = False,
) -> None:
    """Run the LLM on a goal, compile the run into a capability, verify it by replay, and save a draft."""
    from .discovery import run_discovery

    if not (goal or spec):
        raise typer.BadParameter("give --goal or --spec")
    goal_spec = load_model(spec, GoalSpec) if spec else None
    report = asyncio.run(
        run_discovery(
            tenant_id=tenant,
            goal=goal,
            spec=goal_spec,
            verify_inputs=_kv(verify_input),
            headed=headed or None,  # default: headed only when an operator may take over
            operator=_operator(operator),
            max_turns=max_turns,
            vision=not no_vision,
            record_video=video,
        )
    )
    colour = "green" if report.status == "succeeded" and report.capability else "red"
    console.print(
        f"\n[bold {colour}]DISCOVERY {report.status.upper()}[/] {report.code or ''} {report.message or ''}"
    )
    console.print(
        f"  goal: {report.goal}\n  turns: {report.turns}  models: {', '.join(report.models)}  usage: {report.usage}"
    )
    if report.capability:
        console.print(f"  [bold]capability[/] {report.capability} -> {report.capability_path}")
    if report.verification:
        console.print(
            f"  verify-by-replay: {report.verification['status']} (run {report.verification['run_id']})"
        )
    for n in report.compile_notes:
        console.print(f"  note: {n}")
    for f in report.lint_findings:
        console.print(f"  [red]lint[/] {f}")
    console.print(f"[dim]evidence: {report.evidence_dir}[/]")
    raise typer.Exit(0 if report.capability else 1)


# ---------------------------------------------------------------------------------- capabilities


@caps_app.command("list")
def caps_list() -> None:
    t = Table("capability", "status", "effects", "inputs", "outputs", "approved by")
    for c in Registry().all():
        t.add_row(
            c.ref,
            c.status + ("" if c.status != "approved" or c.approval_valid() else " (EDITED)"),
            c.contract.effects,
            ", ".join(c.contract.inputs),
            ", ".join(c.contract.outputs),
            c.approval.approved_by if c.approval else "",
        )
    console.print(t)


@caps_app.command("show")
def caps_show(ref: str) -> None:
    print(model_to_yaml(_load_cap(ref)))


@caps_app.command("validate")
def caps_validate(paths: list[Path]) -> None:
    ok = True
    for p in paths:
        try:
            c = load_model(p, Capability)
            console.print(f"[green]valid[/] {p} ({c.ref}, sha {c.content_sha256()[:12]})")
        except ValueError as e:
            ok = False
            console.print(f"[red]invalid[/] {e}")
    raise typer.Exit(0 if ok else 1)


@caps_app.command("approve")
def caps_approve(ref: str, reviewer: Annotated[str, typer.Option(help="who reviewed it")]) -> None:
    """Mark a capability approved; the approval signs its content hash."""
    c = Registry().approve(ref, reviewer)
    console.print(f"approved {c.ref} by {reviewer}; content sha256 {c.content_sha256()[:16]}…")


@caps_app.command("schema")
def caps_schema(out: Path = Path("schemas/capability.schema.json")) -> None:
    """Export the artifact's JSON Schema (for reviewers, editors and calling agents)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Capability.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    console.print(f"wrote {out}")


# ---------------------------------------------------------------------------------- catalog


@catalog_app.command("export")
def catalog_export(tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")]) -> None:
    """Print the tool definitions a calling agent would see for this tenant."""
    from .catalog import eligible, tool_for

    print(json.dumps([tool_for(c) for c in eligible(tenant)], indent=2))


@catalog_app.command("ask")
def catalog_ask(
    question: str, tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")]
) -> None:
    """A small calling agent answers a question by invoking capabilities (executed by deterministic replay)."""
    from .catalog import ask

    out = asyncio.run(ask(question, tenant))
    for c in out["calls"]:
        console.print(f"  called {c['tool']} -> replay {c['replay_run']} [{c['status']}]")
    console.print(f"\n[bold]answer[/] {out['answer']}\n[dim]evidence: {out['evidence_dir']}[/]")


# ---------------------------------------------------------------------------------- mock bank


def _admin(tenant: str, path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    t = load_tenant(tenant)
    token = os.environ.get("MOCK_ADMIN_TOKEN", "change-me-admin")
    r = httpx.post(t.base_url + path, json=body, headers={"X-Admin-Token": token}, timeout=10)
    r.raise_for_status()
    return r.json()


@mock_app.command("serve")
def mock_serve(
    tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")], port: int | None = None
) -> None:
    """Serve one tenant of the mock bank (blocking)."""
    from urllib.parse import urlparse

    import uvicorn

    from mock_bank.app import create_app

    port = port or urlparse(load_tenant(tenant).base_url).port or 8401
    uvicorn.run(create_app(tenant), host="127.0.0.1", port=port, log_level="warning")


@mock_app.command("fault")
def mock_fault(
    kind: Annotated[str, typer.Argument(help="maintenance | alert | error500 | slow | expire")],
    tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")],
    count: int = 1,
    path: Annotated[str, typer.Option(help="only requests under this path")] = "/core/",
    delay_ms: int = 0,
) -> None:
    """Arm a fault on the mock (test hook; never reachable by the automation's allowlist)."""
    console.print(
        _admin(
            tenant,
            "/__admin/faults",
            {"kind": kind, "count": count, "path_prefix": path, "delay_ms": delay_ms},
        )
    )


@mock_app.command("reset")
def mock_reset(tenant: Annotated[str, typer.Option(help="tenant id (config/tenants/<id>.yaml)")]) -> None:
    console.print(_admin(tenant, "/__admin/reset"))


def main() -> None:
    load_dotenv(repo_root() / ".env")
    load_dotenv()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
