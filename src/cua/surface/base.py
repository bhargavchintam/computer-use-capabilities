"""The seam between "how we perceive and act on a surface" and "the recorded flow".

The runtime, replay engine, checkpoint evaluator and discovery agent are typed against
`Surface` and never import a driver: an element is an opaque handle, a container is a
path of names, and targets are the strategy dicts of models/targets.py. Web is
implemented (`WebSurface`, Playwright). The same protocol maps onto other surfaces:

* desktop: snapshot from UI Automation / AX (ControlType, Name, LabeledBy,
  AutomationId, Grid/Table patterns); containers are window/pane paths; actions via
  Invoke/Value patterns; dialogs are just windows.
* pixel-only (Citrix/VDI): snapshot from OCR + element detection; strategies resolve
  against OCR text anchors; actions by coordinates.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


class NotReady(Exception):
    """The surface is mid-transition (navigation, frame swap). Retry shortly."""


class SessionLost(Exception):
    """The live session is gone (browser/window closed)."""


class StaleRef(Exception):
    """An element ref from an older observation was used after the page changed."""


class ActionFailed(Exception):
    """The control was found but the action could not be performed: disabled, covered,
    not editable, or the requested option does not exist. `before_dispatch` is true when
    the surface knows no input reached the application (it gave up while waiting for the
    control to become actionable)."""

    def __init__(self, message: str, *, before_dispatch: bool = False) -> None:
        super().__init__(message)
        self.before_dispatch = before_dispatch


Element = Any  # an opaque handle owned by the surface (DOM element, UIA element, OCR box)
DialogDecision = Literal["accept", "dismiss"]


@dataclass
class Resolution:
    element: Element
    container: list[str]
    index: int  # which strategy matched (0 = primary)
    strategy: dict[str, Any]
    diagnostics: list[tuple[dict[str, Any], int]]


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
    """Everything the runtime, replay engine and agent may do to a live application."""

    headed: bool
    closed: bool
    # Hooks installed by the runtime.
    on_dialog: Callable[[str, str], Awaitable[DialogDecision]] | None  # (type, message) -> decision
    on_capture: Callable[[dict[str, Any], list[str]], None] | None  # trusted human input
    route_guard: Callable[[str, str], bool] | None  # (url, resource type) -> allowed

    # lifecycle and navigation
    async def start(self) -> None: ...
    async def close(self) -> Path | None: ...
    async def bring_to_front(self) -> None: ...
    async def goto(self, route: str) -> None: ...
    def settled(self, quiet_ms: int) -> bool: ...
    def url_path(self, container: list[str]) -> str | None: ...

    # perception (all text is raw here; the runtime redacts before anything leaves)
    async def snapshot(self) -> PageSnapshot: ...
    async def page_states(self) -> dict[tuple[str, ...], dict[str, Any]]: ...
    async def page_text(self, container: list[str]) -> str | None: ...
    async def red_texts(self, container: list[str]) -> list[str]: ...
    async def doc_id(self, container: list[str]) -> str | None: ...
    async def doc_ids(self) -> dict[tuple[str, ...], str]: ...
    async def screenshot(
        self,
        sensitive_labels: list[str],
        marks: list[tuple[str, list[float]]] | None = None,
        sensitive_values: list[str] | None = None,
    ) -> bytes | None: ...

    # targeting
    async def element_for_ref(self, snap: PageSnapshot, ref: str) -> tuple[list[str], Element]: ...
    async def resolve(
        self, container: list[str], strategies: list[dict[str, Any]]
    ) -> tuple[Resolution | None, list[tuple[dict[str, Any], int]], bool]: ...
    async def strategies_agree(self, container: list[str], a: dict[str, Any], b: dict[str, Any]) -> bool: ...
    async def near_misses(self, container: list[str], strategy: dict[str, Any]) -> list[dict[str, Any]]: ...
    async def table_present(self, container: list[str], headers: list[str]) -> bool: ...
    async def describe(self, el: Element) -> dict[str, Any]: ...
    async def read_text(self, el: Element) -> str: ...
    async def read_table(self, el: Element, columns: dict[str, str]) -> list[dict[str, str | None]]: ...
    async def table_of(self, el: Element) -> Element | None: ...

    # actions: real input events
    async def click(self, el: Element) -> None: ...
    async def fill(self, el: Element, value: str) -> None: ...
    async def select(self, el: Element, label: str) -> None: ...
    async def press(self, el: Element, key: str) -> None: ...


def open_surface(kind: str, base_url: str, *, headed: bool, video_dir: Path | None = None) -> Surface:
    """The surface named by a capability's `implementation.app.surface`."""
    if kind == "web":
        from .web import WebSurface

        return WebSurface(base_url, headed=headed, video_dir=video_dir)
    raise NotImplementedError(f"surface {kind!r} is designed (see REPORT) but not implemented")
