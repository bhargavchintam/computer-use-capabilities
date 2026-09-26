"""The seam between "how we perceive and act on a surface" and "the recorded flow".

The replay engine, runtime guard and discovery agent only use this protocol.
Web is implemented (`WebSurface`). The same shape maps onto other surfaces:

* desktop: snapshot from UI Automation / AX (ControlType, Name, LabeledBy,
  AutomationId, Grid/Table patterns); containers are window/pane paths;
  actions via Invoke/Value patterns.
* pixel-only (Citrix/VDI): snapshot from OCR + element detection; strategies
  resolve against OCR text anchors; actions by coordinates.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class NotReady(Exception):
    """The surface is mid-transition (navigation, frame swap). Retry shortly."""


class SessionLost(Exception):
    """The live session is gone (browser/window closed)."""


class StaleRef(Exception):
    """An element ref from an older observation was used after the page changed."""


@dataclass
class FrameSnapshot:
    container: list[str]
    url_path: str
    title: str
    doc_id: str
    frameset: bool
    items: list[dict[str, Any]]
    offset: tuple[float, float] = (0.0, 0.0)


@dataclass
class PageSnapshot:
    frames: list[FrameSnapshot]
    ref_index: dict[str, tuple[tuple[str, ...], str]] = field(
        default_factory=dict
    )  # ref -> (container, doc_id)

    def frame(self, container: list[str]) -> FrameSnapshot | None:
        return next((f for f in self.frames if f.container == list(container)), None)

    def signature(self) -> str:
        """Identity of the page state (which documents are loaded), for loop/no-progress checks."""
        return "|".join(f"{'/'.join(f.container)}={f.url_path}#{f.doc_id}" for f in self.frames)


def iter_refs(item: dict[str, Any]) -> Iterator[str]:
    if "ref" in item:
        yield item["ref"]
    for pair in item.get("pairs", []):
        yield pair["ref"]
    for row in item.get("rows", []):
        for cell in row:
            yield cell["ref"]


class Surface(Protocol):
    async def snapshot(self) -> PageSnapshot: ...

    async def page_text(self, container: list[str]) -> str | None: ...

    async def doc_id(self, container: list[str]) -> str | None: ...

    async def screenshot(
        self, sensitive_labels: list[str], marks: list[tuple[str, list[float]]] | None = None
    ) -> bytes | None: ...
