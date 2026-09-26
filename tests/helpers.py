"""Test doubles for the human side of the loop. Clearly simulated: the evidence handoff is a real person."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from cua.control import ControlState, SessionController
from cua.runtime import RuntimeSession


def scripted_operator(
    *, approval: str | None = None, pin: str | None = None, note: str = "handled by scripted operator"
) -> Callable[[SessionController, RuntimeSession], Awaitable[None]]:
    """Answers intervention requests like a person would: approve/reject, or take control, act, hand back."""

    async def hook(controller: SessionController, session: RuntimeSession) -> None:
        while True:
            await asyncio.sleep(0.1)
            req = controller.active
            if req is None or req.status != "pending":
                continue
            if req.kind == "approval" and approval:
                controller.decide(req.id, approval, "scripted-supervisor", note)  # type: ignore[arg-type]
            elif req.kind == "human_required" and pin:
                controller.decide(req.id, "take_control", "scripted-supervisor")
                while controller.state != ControlState.HUMAN:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(1.2)  # after the grace window, like a person reading the screen
                s = session.surface
                pin_box, _, _ = await s.resolve(
                    ["frame:work"], [{"kind": "label", "role": "textbox", "label": "Supervisor PIN:"}]
                )
                assert pin_box is not None
                await pin_box.element.click()
                await pin_box.element.type(pin, delay=30)
                ok, _, _ = await s.resolve(
                    ["frame:work"], [{"kind": "role_name", "role": "button", "name": "Approve"}]
                )
                assert ok is not None
                await ok.element.click()
                await asyncio.sleep(1.0)
                controller.decide(req.id, "hand_back", "scripted-supervisor", note)

    return hook
