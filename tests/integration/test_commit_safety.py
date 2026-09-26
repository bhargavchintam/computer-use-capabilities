"""A commit is sent at most once, whatever happens around it.

Each test counts the form submissions the bank actually received, because a duplicate
submit can be refused by the app (and so leave a single receipt) while still being a bug.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cua.control import ControlState, SessionController
from cua.models import Capability
from cua.replay import run_replay
from cua.runtime import RuntimeSession
from tests.conftest import Bank

pytestmark = pytest.mark.integration
INPUTS = {
    "member_number": "10042",
    "share_type": "Holiday Club",
    "initial_deposit": "250.00",
    "nickname": "Gift fund",
}
CONFIRM = "/core/member/10042/newshare/confirm"


def confirm_posts(bank: Bank) -> int:
    return sum(1 for p in bank.state("pinecrest")["posts"] if p == CONFIRM)


async def test_a_slow_commit_response_is_waited_for_not_resent(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "slow_response", path=CONFIRM, delay_ms=4000)
    r = await run_replay(share_cap, tenant_id="pinecrest", inputs=INPUTS, approve=True, runs_root=tmp_path)
    assert r.status == "succeeded", r.error
    assert r.side_effect == "committed" and confirm_posts(bank) == 1
    assert len(bank.state("pinecrest")["receipts"]) == 1


async def test_a_lost_commit_response_is_unknown_and_never_retried(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "slow_response", path=CONFIRM, delay_ms=15000)  # beyond the step timeout
    r = await run_replay(share_cap, tenant_id="pinecrest", inputs=INPUTS, approve=True, runs_root=tmp_path)
    assert r.status == "failed" and r.error is not None
    assert r.side_effect == "unknown" and r.retry_safe is False
    assert r.error.transient is False, "never suggest a retry that could commit twice"
    assert confirm_posts(bank) == 1
    persisted = json.loads((Path(r.evidence_dir or "") / "result.json").read_text())
    assert persisted["side_effect"] == "unknown" and persisted["error"]["transient"] is False


async def test_a_person_grabbing_the_window_mid_commit_never_causes_a_second_click(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    """While the Confirm response is in flight, a person touches the live window (implicit
    takeover) and hands back while the review page, with its Confirm button, is still showing
    (the response outlasts the resync wait). Before the fix, replay found Confirm again and
    clicked it a second time. The oracle is replay's own clicks: a browser may silently drop a
    second submit of a form whose first submit is still pending, so POST counts can hide it."""
    bank.fault("pinecrest", "slow_response", path=CONFIRM, delay_ms=20000)

    async def person(controller: SessionController, session: RuntimeSession) -> None:
        while confirm_posts(bank) == 0:
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.3)  # past the window in which input is attributed to automation
        # A click on the review page, delivered through the same entry point as the in-page listener.
        controller.on_capture(
            {"type": "click", "describe": {"role": "cell", "label": "Nickname:"}}, ["frame:work"]
        )
        assert controller.state == ControlState.HUMAN and controller.active is not None
        await asyncio.sleep(0.5)
        controller.decide(controller.active.id, "hand_back", "local-operator", "only looked")

    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs=INPUTS,
        approve=True,
        operator="scripted",
        operator_hook=person,
        runs_root=tmp_path,
    )
    events = [
        json.loads(line) for line in (Path(r.evidence_dir or "") / "events.jsonl").read_text().splitlines()
    ]
    clicks = [e for e in events if e["type"] == "action_performed" and e.get("step_id") == "click_confirm"]
    assert len(clicks) == 1, "the commit control was clicked again after the hand-back"
    assert r.status == "failed" and r.error is not None and r.error.code == "RESYNC_FAILED"
    assert r.side_effect == "unknown" and r.retry_safe is False
    assert [i.kind for i in r.interventions] == ["implicit_takeover"]


async def test_a_commit_found_only_by_a_fallback_locator_is_refused(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    """A renamed Confirm button that only the attribute fallback still finds is a weak match:
    a commit acts only on its primary locator, or a fallback another strategy confirms."""
    data = share_cap.model_dump(mode="json", exclude_none=True)
    confirm = next(s for s in data["implementation"]["steps"] if s["id"] == "click_confirm")
    confirm["target"]["strategies"][0] = {"kind": "role_name", "role": "button", "name": "Confirm Share"}
    cap = Capability.model_validate(data)
    r = await run_replay(cap, tenant_id="pinecrest", inputs=INPUTS, approve=True, runs_root=tmp_path)
    assert r.status == "failed" and r.error is not None and r.error.code == "TARGET_NOT_FOUND"
    assert "weak match" in r.error.message
    assert r.side_effect == "not_committed" and confirm_posts(bank) == 0


async def test_an_option_the_ui_does_not_offer_is_a_classified_failure(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    """A control that cannot take the action is a result with evidence, never a crash."""
    data = share_cap.model_dump(mode="json", exclude_none=True)
    select = next(s for s in data["implementation"]["steps"] if s["action"] == "select")
    select["value"] = {"literal": "Platinum Club"}
    cap = Capability.model_validate(data)
    r = await run_replay(cap, tenant_id="pinecrest", inputs=INPUTS, approve=True, runs_root=tmp_path)
    assert r.status == "failed" and r.error is not None and r.error.code == "TARGET_NOT_ACTIONABLE"
    assert r.error.evidence and r.side_effect == "not_committed"
    assert (Path(r.evidence_dir or "") / "result.json").exists()
