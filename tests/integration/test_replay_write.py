"""Replay of the write capability: commit gating, side-effect reporting and the same-session handoff."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cua.models import Capability
from cua.replay import run_replay
from tests.conftest import Bank
from tests.helpers import scripted_operator

pytestmark = pytest.mark.integration
BASE = {"member_number": "10042", "share_type": "Holiday Club", "nickname": "Gift fund"}


async def test_commit_with_invocation_approval(bank: Bank, share_cap: Capability, tmp_path: Path) -> None:
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "250.00"},
        approve=True,
        runs_root=tmp_path,
    )
    assert r.status == "succeeded" and r.side_effect == "committed" and not r.retry_safe
    assert (
        r.outputs and r.outputs["confirmation_number"].startswith("CN") and r.outputs["new_share_id"] == "S51"
    )
    assert len(bank.state("pinecrest")["receipts"]) == 1


async def test_commit_without_approval_parks_and_commits_nothing(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        share_cap, tenant_id="pinecrest", inputs={**BASE, "initial_deposit": "250.00"}, runs_root=tmp_path
    )
    assert r.status == "needs_human" and r.side_effect == "not_committed" and r.retry_safe
    assert r.parked and (Path(r.evidence_dir or "") / r.parked.request_path).exists()
    assert bank.state("pinecrest")["receipts"] == []


async def test_operator_rejection_is_a_business_outcome(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "250.00"},
        operator="scripted",
        operator_hook=scripted_operator(approval="reject"),
        runs_root=tmp_path,
    )
    assert r.status == "business_outcome" and r.outcome and r.outcome.code == "APPROVAL_DENIED"
    assert r.side_effect == "not_committed" and bank.state("pinecrest")["receipts"] == []


async def test_operator_approval_then_commit(bank: Bank, share_cap: Capability, tmp_path: Path) -> None:
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "250.00"},
        operator="scripted",
        operator_hook=scripted_operator(approval="approve"),
        runs_root=tmp_path,
    )
    assert r.status == "succeeded" and r.side_effect == "committed"
    assert r.interventions[0].decision == "approve" and r.interventions[0].operator == "scripted-supervisor"


async def test_pre_commit_check_blocks_a_mismatched_review_page(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    data = share_cap.model_dump(mode="json", exclude_none=True)
    confirm = next(s for s in data["implementation"]["steps"] if s["id"] == "click_confirm")
    confirm["pre"].append(
        {"text_visible": {"container": ["frame:work"], "text": "Something the page does not show"}}
    )
    cap = Capability.model_validate(data)
    r = await run_replay(
        cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "250.00"},
        approve=True,
        runs_root=tmp_path,
    )
    assert r.status == "failed" and r.error and r.error.code == "CHECKPOINT_FAILED"
    assert r.side_effect == "not_committed" and bank.state("pinecrest")["receipts"] == []


async def test_drift_into_a_commit_control_is_blocked(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    """A step recorded as a read that now resolves to a commit-class control must not act."""
    data = share_cap.model_dump(mode="json", exclude_none=True)
    cont = next(s for s in data["implementation"]["steps"] if s["id"] == "click_continue")
    cont["expect"] = []
    data["implementation"]["steps"] = [
        s
        for s in data["implementation"]["steps"]
        if s["id"]
        in (
            "click_member_inquiry",
            "fill_member_number",
            "click_search",
            "click_open_new_share",
            "select_share_type",
            "fill_initial_deposit",
            "fill_nickname",
            "click_continue",
        )
    ]
    # simulate drift: a later "read" step now finds the Confirm button
    drifted = dict(
        cont,
        id="click_next",
        target={
            "container": ["frame:work"],
            "strategies": [{"kind": "role_name", "role": "button", "name": "Confirm"}],
        },
    )
    data["implementation"]["steps"].append(drifted)
    data["contract"]["outputs"] = {}
    data["contract"]["outcomes"] = {}
    data["contract"]["effects"], data["contract"]["idempotent"] = "read_only", True
    data["contract"].pop("example", None)
    data["implementation"]["outcome_detectors"] = {}
    data["implementation"]["success"] = {
        "text_visible": {"container": ["frame:work"], "text": "Review New Share"}
    }
    cap = Capability.model_validate(data)
    r = await run_replay(
        cap, tenant_id="pinecrest", inputs={**BASE, "initial_deposit": "250.00"}, runs_root=tmp_path
    )
    assert r.status == "failed" and r.error and r.error.code == "POLICY_DENIED"
    assert bank.state("pinecrest")["receipts"] == []


async def test_supervisor_pin_handoff_on_the_same_session(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    pin = os.environ["MOCK_SUPERVISOR_PIN"]
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "7500.00", "nickname": "Trip fund"},
        approve=True,
        operator="scripted",
        operator_hook=scripted_operator(pin=pin),
        runs_root=tmp_path,
    )
    assert r.status == "succeeded", r.error
    assert r.side_effect == "committed"
    assert next(s for s in r.steps if s.step_id == "click_confirm").status == "completed_by_human"
    iv = r.interventions[0]
    assert iv.kind == "human_required" and iv.decision == "hand_back"
    targets = [a.target for a in iv.human_actions]
    assert 'textbox "Supervisor PIN:"' in targets and 'button "Approve"' in targets
    assert all(a.value in (None, "<not captured: secret field>") for a in iv.human_actions)
    receipts = bank.state("pinecrest")["receipts"]
    assert len(receipts) == 1 and receipts[0]["supervisor"] is True
    run_dir = Path(r.evidence_dir or "")
    for p in run_dir.rglob("*"):
        if p.is_file() and p.suffix != ".png":
            assert pin not in p.read_text(errors="ignore"), f"PIN leaked into {p}"
    events = (run_dir / "events.jsonl").read_text()
    for state in ('"to_state": "awaiting_human"', '"to_state": "human"', '"to_state": "automation"'):
        assert state in events
