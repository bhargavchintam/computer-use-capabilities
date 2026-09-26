"""Discovery end to end with a deterministic stand-in model (no network).

The real-model run lives in /evidence; these tests pin down the machinery around it:
grounding, compilation, linting, verify-by-replay, and that runtime interruptions
never leak into the recorded flow.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cua.agent.llm import ScriptedLLM
from cua.configio import load_model
from cua.discovery import run_discovery
from cua.models import Capability, GoalSpec, InputSpec, OutputSpec
from cua.registry import Registry
from cua.replay import run_replay
from tests.conftest import ROOT, Bank, approved

pytestmark = pytest.mark.integration

SPEC = GoalSpec(
    capability_id="member.get_share_balance",
    title="Look up a member's share balances",
    goal="Look up member {{member_number}} and read their current savings balance and list of shares",
    inputs={
        "member_number": InputSpec(
            type="string", description="Member number", pattern="^[0-9]{5}$", sensitivity="pii_identifier"
        )
    },
    outputs={
        "savings_balance": OutputSpec(
            type="money", description="PRIMARY SAVINGS balance", currency="USD", sensitivity="confidential"
        ),
        "shares": OutputSpec(
            type="table",
            description="All shares",
            sensitivity="confidential",
            columns={"share_id": "identifier", "description": "string", "balance": "money"},
        ),
    },
    sample_inputs={"member_number": "10042"},
)


def operator_like_policy() -> Any:
    """What a competent model does on this app, expressed as a pure function of the observation."""
    done: set[str] = set()

    def policy(text: str, turn: int) -> tuple[str, dict[str, Any]]:
        def ref(pattern: str) -> str | None:
            m = re.search(r"\[(e\d+)\][^\n]*?" + pattern, text)
            return m.group(1) if m else None

        if "**Member Summary**" in text:
            if "bal" not in done:
                row = next(line for line in text.splitlines() if "PRIMARY SAVINGS" in line)
                done.add("bal")
                return "record_output", {
                    "name": "savings_balance",
                    "ref": re.findall(r"\[(e\d+)\]", row)[2],
                    "reason": "Balance column of the PRIMARY SAVINGS row",
                }
            if "tbl" not in done:
                done.add("tbl")
                return "record_output", {
                    "name": "shares",
                    "ref": ref(r"table \("),
                    "reason": "The shares table",
                }
            return "finish", {
                "summary": "Read balance and shares",
                "success_ref": ref(r"\*\*Member Summary\*\*"),
            }
        box = re.search(r'\[(e\d+)\] textbox label="Member #:" value="([^"]*)"', text)
        if box and not box.group(2):
            return "type_text", {
                "ref": box.group(1),
                "text": "{{member_number}}",
                "reason": "Enter the member number",
            }
        if box:
            return "click", {"ref": ref(r'button "Search"'), "reason": "Run the search"}
        return "click", {"ref": ref(r'link "Member Inquiry"'), "reason": "Open Member Inquiry from the menu"}

    return policy


async def _discover(tmp_path: Path) -> Any:
    return await run_discovery(
        tenant_id="pinecrest",
        spec=SPEC,
        verify_inputs={"member_number": "10077"},
        llm=ScriptedLLM(operator_like_policy()),
        runs_root=tmp_path / "runs",
        registry=Registry(tmp_path / "registry"),
    )


async def test_discovery_compiles_a_verified_capability(bank: Bank, tmp_path: Path) -> None:
    report = await _discover(tmp_path)
    assert report.status == "succeeded" and report.capability, report
    assert report.verification and report.verification["status"] == "succeeded"
    assert not report.lint_findings
    cap = load_model(Path(report.capability_path or ""), Capability)
    assert [s.id for s in cap.implementation.steps] == [
        "click_member_inquiry",
        "fill_member_number",
        "click_search",
        "extract_savings_balance",
        "extract_shares",
    ]
    fill = cap.step("fill_member_number")
    assert fill.value is not None and fill.value.model_dump() == {"param": "member_number"}
    bal = cap.step("extract_savings_balance").target.strategies[0].model_dump()
    assert bal["kind"] == "table_cell" and bal["row"] == {
        "column": "Description",
        "equals": "PRIMARY SAVINGS",
    }
    assert cap.contract.effects == "read_only"
    assert set(cap.contract.outcomes) == {"MEMBER_NOT_FOUND", "ACCOUNT_RESTRICTED", "INPUT_REJECTED_BY_APP"}
    text = Path(report.capability_path or "").read_text()
    assert "10042" not in text and "12,450" not in text and "12450" not in text
    assert cap.provenance.verified_by_runs == [report.verification["run_id"]]


async def test_runtime_interruptions_never_enter_the_flow(bank: Bank, tmp_path: Path) -> None:
    bank.fault("pinecrest", "maintenance", path="/core/inquiry")
    report = await _discover(tmp_path)
    assert report.status == "succeeded" and report.capability
    cap = load_model(Path(report.capability_path or ""), Capability)
    assert all("acknowledge" not in s.id for s in cap.implementation.steps)
    events = (Path(report.evidence_dir or "") / "events.jsonl").read_text()
    assert '"detector": "maintenance_notice"' in events and '"type": "recovery"' in events


async def test_discovered_capability_generalizes_to_another_tenant_via_overlay(
    bank: Bank, tmp_path: Path
) -> None:
    report = await _discover(tmp_path)
    cap = approved(load_model(Path(report.capability_path or ""), Capability))
    r = await run_replay(cap, tenant_id="lakeside", inputs={"member_number": "20064"}, runs_root=tmp_path)
    assert r.status == "succeeded", r.error
    assert r.outputs and r.outputs["savings_balance"]["amount"] == "18300.75"


def test_replay_never_needs_the_model_sdk(bank: Bank, tmp_path: Path) -> None:
    fixture = ROOT / "tests/fixtures/capabilities/acmecore/acmecore.member.get_share_balance/1.0.0.yaml"
    code = f"""
import sys, asyncio, json
sys.modules['anthropic'] = None  # any import of the model SDK now fails
from cua.configio import load_model
from cua.models import Capability
from cua.replay import run_replay
cap = load_model(__import__('pathlib').Path({str(fixture)!r}), Capability)
r = asyncio.run(run_replay(cap, tenant_id='pinecrest', inputs={{'member_number': '10042'}},
                           runs_root=__import__('pathlib').Path({str(tmp_path)!r})))
print(json.dumps({{'status': r.status, 'loaded': [m for m in sys.modules if m.startswith('cua.agent')]}}))
"""
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=os.environ.copy(), timeout=120
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"status": "succeeded", "loaded": []}
