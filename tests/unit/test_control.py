from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cua.control import ControlLost, ControlState, SessionController
from cua.evidence import RunRecorder
from cua.redaction import Redactor


def controller(tmp_path: Path, operator: bool = True, timeout: float = 5.0) -> SessionController:
    redactor = Redactor()
    return SessionController(
        RunRecorder(tmp_path, "test", redactor),
        redactor,
        operator_available=operator,
        handoff_timeout_s=timeout,
    )


async def test_approve_moves_the_lease_back_with_a_new_epoch(tmp_path: Path) -> None:
    c = controller(tmp_path)
    epoch0 = c.epoch
    task = asyncio.create_task(c.escalate(kind="approval", reason="commit", allowed=["approve", "reject"]))
    await asyncio.sleep(0.05)
    assert c.state == ControlState.AWAITING_HUMAN
    with pytest.raises(ControlLost):
        c.assert_automation()
    assert c.decide(c.active.id, "approve", "alice")[0]  # type: ignore[union-attr]
    res = await task
    assert res.kind == "approve" and c.state == ControlState.AUTOMATION and c.epoch == epoch0 + 2
    with pytest.raises(ControlLost, match="changed hands"):
        c.assert_automation(epoch0)  # a decision made before the handoff is stale


async def test_take_control_then_hand_back(tmp_path: Path) -> None:
    c = controller(tmp_path)
    task = asyncio.create_task(
        c.escalate(kind="human_required", reason="pin", allowed=["take_control", "abort"])
    )
    await asyncio.sleep(0.05)
    rid = c.active.id  # type: ignore[union-attr]
    c.decide(rid, "take_control", "bob")
    await asyncio.sleep(0.05)
    assert c.state == ControlState.HUMAN and c.actor == "human:bob"
    c.on_capture(
        {
            "type": "change",
            "secret": True,
            "value": None,
            "describe": {"role": "textbox", "label": "Supervisor PIN:", "control": {"secret": True}},
        },
        ["frame:work"],
    )
    c.decide(rid, "hand_back", "bob", "entered PIN")
    res = await task
    assert res.kind == "hand_back" and c.state == ControlState.AUTOMATION
    assert res.human_actions[0].value == "<not captured: secret field>"


async def test_no_operator_parks_immediately(tmp_path: Path) -> None:
    c = controller(tmp_path, operator=False)
    res = await c.escalate(kind="approval", reason="commit", allowed=["approve"])
    assert res.kind == "parked" and c.state == ControlState.AUTOMATION
    assert (tmp_path / c.recorder.run_id / "interventions" / f"{res.request.id}.json").exists()


async def test_deadline_expires(tmp_path: Path) -> None:
    c = controller(tmp_path, timeout=0.3)
    res = await c.escalate(kind="stuck", reason="?", allowed=["take_control"])
    assert res.kind == "timeout" and res.request.status == "expired"


async def test_human_input_while_automation_holds_the_lease_is_an_implicit_takeover(tmp_path: Path) -> None:
    c = controller(tmp_path)
    c._last_action_end = 0.0
    c.on_capture({"type": "click", "describe": {"role": "link", "name": "Reports"}}, ["frame:nav"])
    assert c.state == ControlState.HUMAN and c.active and c.active.kind == "implicit_takeover"
    with pytest.raises(ControlLost):
        async with c.automated_action():
            pass


async def test_disallowed_decisions_are_refused(tmp_path: Path) -> None:
    c = controller(tmp_path)
    task = asyncio.create_task(
        c.escalate(kind="human_required", reason="pin", allowed=["take_control", "abort"])
    )
    await asyncio.sleep(0.05)
    ok, _ = c.decide(c.active.id, "approve", "mallory")  # type: ignore[union-attr]
    assert not ok
    c.decide(c.active.id, "abort", "alice")  # type: ignore[union-attr]
    assert (await task).kind == "abort" and c.state == ControlState.CLOSED
