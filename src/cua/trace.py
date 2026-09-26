"""What happened during discovery, grounded: every action carries the validated
candidate targets computed at the moment it happened, plus page state before
and after. The compiler turns this into a capability; the raw model transcript
is never needed (and never stored in the artifact)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class FrameState:
    container: tuple[str, ...]
    url_path: str
    doc_id: str
    emph: list[str]  # bold/large texts: titles, section headers
    text: str  # full visible text (in memory only; never persisted raw)
    fields: list[tuple[str, str]] = field(default_factory=list)  # label/value pairs (memory only)

    @property
    def title(self) -> str:
        return self.emph[0] if self.emph else ""


@dataclass
class PageState:
    frames: dict[tuple[str, ...], FrameState] = field(default_factory=dict)

    @classmethod
    def from_probe(cls, states: dict[tuple[str, ...], dict[str, Any]]) -> PageState:
        return cls(
            {
                c: FrameState(
                    c,
                    s["url_path"],
                    s["doc_id"],
                    list(s["emph"]),
                    s["text"],
                    [(str(a), str(b)) for a, b in s.get("fields") or []],
                )
                for c, s in states.items()
            }
        )

    def key(self) -> tuple[tuple[Any, ...], ...]:
        """Page identity for loop detection: which screen is in which frame (not which load)."""
        return tuple((c, f.url_path, f.title) for c, f in sorted(self.frames.items()))

    def changed_containers(self, before: PageState) -> list[tuple[str, ...]]:
        return [
            c for c, f in self.frames.items() if c not in before.frames or before.frames[c].doc_id != f.doc_id
        ]


@dataclass
class TraceStep:
    index: int
    actor: Literal["agent", "human"]
    action: str  # click | fill | select | press_key | extract | extract_table
    container: list[str]
    describe: dict[str, Any]  # in-page describe(): candidates (validated), fingerprint, control info
    reason: str = ""
    effect: str = "read"
    value_text: str | None = None  # agent: placeholder text; human: raw value (memory only)
    key: str | None = None
    output: str | None = None
    columns: dict[str, str] | None = None
    before: PageState | None = None
    after: PageState | None = None
    approved_by: str | None = None
    superseded: bool = False  # a session restart made this step irrelevant

    def summary(self) -> dict[str, Any]:
        d = self.describe or {}
        return {
            "index": self.index,
            "actor": self.actor,
            "action": self.action,
            "container": self.container,
            "target": {
                "role": d.get("role"),
                "name": d.get("name"),
                "label": d.get("label"),
                "tag": d.get("tag"),
            },
            "strategies": [c["strategy"] for c in d.get("candidates", []) if c.get("unique")],
            "effect": self.effect,
            "value": self.value_text
            if self.actor == "agent"
            else ("<human input>" if self.value_text else None),
            "key": self.key,
            "output": self.output,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "superseded": self.superseded,
            "before": [list(k) for k in self.before.key()] if self.before else None,
            "after": [list(k) for k in self.after.key()] if self.after else None,
        }
