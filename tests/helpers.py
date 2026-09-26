"""Test doubles for the human side of the loop and for the model. Clearly simulated: the evidence
handoff is a real person, and the evidence discovery runs use the real model."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from typing import Any

from cua.agent.llm import LLMTurn, ScriptedLLM
from cua.control import ControlState, SessionController
from cua.runtime import RuntimeSession


def scripted_operator(
    *,
    approval: str | None = None,
    pin: str | None = None,
    unstick: bool = False,
    note: str = "handled by scripted operator",
) -> Callable[[SessionController, RuntimeSession], Awaitable[None]]:
    """Answers intervention requests like a person would: approve/reject, or take control, act, hand back."""

    async def take_control(controller: SessionController, req_id: str) -> None:
        controller.decide(req_id, "take_control", "scripted-supervisor")
        while controller.state != ControlState.HUMAN:
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.2)  # after the grace window, like a person reading the screen

    async def hook(controller: SessionController, session: RuntimeSession) -> None:
        while True:
            await asyncio.sleep(0.1)
            req = controller.active
            if req is None or req.status != "pending":
                continue
            if req.kind == "approval" and approval:
                controller.decide(req.id, approval, "scripted-supervisor", note)  # type: ignore[arg-type]
            elif req.kind == "stuck" and unstick:
                await take_control(controller, req.id)
                controller.decide(req.id, "hand_back", "scripted-supervisor", note)
            elif req.kind == "human_required" and pin:
                await take_control(controller, req.id)
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


class ApiShapeCheckingLLM(ScriptedLLM):
    """A scripted model that also enforces what the Messages API enforces on a real request, so the
    loop's history handling is tested offline: the history is append-only (thinking blocks are bound
    to the exact prefix before them), every tool call is answered by its tool_result in the next
    message, and a mid-conversation system message follows a user message and is either the last
    entry or followed by an assistant turn."""

    supports_system_messages = True

    def __init__(self, policy: Callable[[str, int], tuple[str, dict[str, Any]] | str]) -> None:
        super().__init__(policy)
        self.requests: list[list[dict[str, Any]]] = []
        self.violations: list[str] = []

    async def step(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMTurn:
        n = len(self.requests) + 1
        if self.requests and messages[: len(self.requests[-1])] != self.requests[-1]:
            self.violations.append(f"request {n}: earlier messages were edited (history must be append-only)")
        if not messages or messages[0]["role"] != "user":
            self.violations.append(f"request {n}: the first message must be a user message")
        for i, msg in enumerate(messages):
            if msg["role"] == "system":
                if i == 0 or messages[i - 1]["role"] != "user":
                    self.violations.append(f"request {n}: system message {i} does not follow a user message")
                if i != len(messages) - 1 and messages[i + 1]["role"] != "assistant":
                    self.violations.append(
                        f"request {n}: system message {i} is followed by a non-assistant turn"
                    )
            if msg["role"] == "assistant":
                ids = [b.id for b in msg["content"] if getattr(b, "type", None) == "tool_use"]
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                answered = (
                    {b.get("tool_use_id") for b in nxt["content"] if isinstance(b, dict)}
                    if nxt and nxt["role"] == "user" and isinstance(nxt["content"], list)
                    else set()
                )
                missing = [x for x in ids if x not in answered]
                if missing:
                    self.violations.append(f"request {n}: tool calls {missing} have no tool_result next")
        self.requests.append(copy.deepcopy(messages))
        return await super().step(system=system, tools=tools, messages=messages)
