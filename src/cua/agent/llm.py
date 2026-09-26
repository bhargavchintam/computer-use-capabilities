"""Model clients for discovery. Replay never imports this module.

`AnthropicLLM` drives Claude through a hand-written tool-use loop (see loop.py):
we own the loop because every action must pass the policy gate, the control
lease and the runtime guard, and every turn is logged as evidence.
`ScriptedLLM` is a deterministic stand-in so the loop can be tested offline.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMTurn:
    content: list[Any]  # content blocks, appended to history exactly as returned
    stop_reason: str
    model: str
    id: str
    usage: dict[str, Any] = field(default_factory=dict)
    thinking: str = ""


class LLMClient(Protocol):
    model: str
    supports_system_messages: bool

    async def step(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMTurn: ...


def _client_kwargs() -> dict[str, Any]:
    headers = {}
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace:  # org-level keys must name the workspace on every request
        headers["anthropic-workspace-id"] = workspace
    return {"default_headers": headers or None, "max_retries": 3, "timeout": 300.0}


def anthropic_client() -> Any:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(**_client_kwargs())


class AnthropicLLM:
    def __init__(self, model: str | None = None, *, effort: str = "high", max_tokens: int = 16000) -> None:
        self.model = model or os.environ.get("CUA_MODEL", DEFAULT_MODEL)
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic_client()
        # Mid-conversation system messages: Opus/Fable family, not Sonnet 5.
        self.supports_system_messages = "sonnet" not in self.model and "haiku" not in self.model

    def _fallback_kwargs(self) -> dict[str, Any]:
        if self.model.startswith(("claude-opus-5", "claude-fable")):
            return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        return {}

    async def step(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMTurn:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system}],
            tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            **self._fallback_kwargs(),
        )
        thinking = " ".join(
            getattr(b, "thinking", "") or "" for b in resp.content if getattr(b, "type", "") == "thinking"
        )
        usage = resp.usage.model_dump(exclude_none=True) if getattr(resp, "usage", None) else {}
        return LLMTurn(
            list(resp.content), resp.stop_reason or "", resp.model, resp.id, usage, thinking.strip()
        )


class ScriptedLLM:
    """Test double: a policy maps the latest observation text to exactly one tool call."""

    supports_system_messages = False

    def __init__(
        self, policy: Callable[[str, int], tuple[str, dict[str, Any]]], model: str = "scripted"
    ) -> None:
        self.policy = policy
        self.model = model
        self.turn = 0

    async def step(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMTurn:
        self.turn += 1
        last = _last_text(messages)
        name, args = self.policy(last, self.turn)
        block = SimpleNamespace(type="tool_use", id=f"toolu_scripted_{self.turn}", name=name, input=args)
        return LLMTurn([block], "tool_use", self.model, f"msg_scripted_{self.turn}", {}, "")


def _last_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg["role"] not in ("user", "system"):
            continue
        content = msg["content"]
        if isinstance(content, str):
            return content
        texts = []
        for block in content:
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_result":
                texts.extend(c["text"] for c in block.get("content", []) if c.get("type") == "text")
        if texts:
            return "\n".join(texts)
    return ""
