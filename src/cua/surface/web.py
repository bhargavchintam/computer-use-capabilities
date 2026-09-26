"""Playwright implementation of the surface: one live Chromium session on one tenant."""

from __future__ import annotations

import io
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PWError

from .base import FrameSnapshot, NotReady, PageSnapshot, SessionLost, StaleRef, iter_refs

BUNDLE = (Path(__file__).parent / "cu_bundle.js").read_text(encoding="utf-8")
MASK_CSS = (
    "[data-cu-mask]{color:transparent!important;background:#2f2f2f!important;"
    "text-shadow:none!important;-webkit-text-fill-color:transparent!important;}"
)
_TRANSIENT = ("context was destroyed", "detached", "cannot find context", "navigat", "no frame for given id")
_CLOSED = ("has been closed", "target closed", "browser has disconnected")


def _classify(e: PWError) -> Exception:
    msg = str(e).lower()
    if any(s in msg for s in _CLOSED):
        return SessionLost(str(e))
    if any(s in msg for s in _TRANSIENT):
        return NotReady(str(e))
    return e


@dataclass
class Resolution:
    frame: Frame
    element: ElementHandle
    index: int  # which strategy matched (0 = primary)
    strategy: dict[str, Any]
    diagnostics: list[tuple[dict[str, Any], int]]


class WebSurface:
    def __init__(
        self,
        base_url: str,
        *,
        headed: bool = False,
        viewport: tuple[int, int] = (1280, 860),
        video_dir: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headed = headed
        self.viewport = viewport
        self.video_dir = video_dir
        # Hooks installed by the runtime.
        self.on_dialog: Callable[[Dialog], Awaitable[None]] | None = None
        self.on_capture: Callable[[dict[str, Any], list[str]], None] | None = None
        self.route_guard: Callable[[str, str], bool] | None = None
        self.blocked_requests: list[dict[str, str]] = []
        self.closed = False
        self._inflight_docs = 0
        self._last_nav = time.monotonic()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def page(self) -> Page:
        if self._page is None:
            raise SessionLost("surface not started")
        return self._page

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        w, h = self.viewport
        launch_args = [f"--window-size={w + 40},{h + 120}"] if self.headed else []
        self._browser = await self._pw.chromium.launch(headless=not self.headed, args=launch_args)
        ctx_kwargs: dict[str, Any] = {
            "service_workers": "block",  # route() cannot see service-worker traffic
            "accept_downloads": False,
            "locale": "en-US",
        }
        if self.headed:
            ctx_kwargs["no_viewport"] = True  # the operator can resize the real window
        else:
            ctx_kwargs["viewport"] = {"width": w, "height": h}
        if self.video_dir:
            ctx_kwargs["record_video_dir"] = str(self.video_dir)
            ctx_kwargs["record_video_size"] = {"width": w, "height": h}
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        await self._ctx.add_init_script(script=BUNDLE)
        await self._ctx.expose_binding("__cuReport", self._capture_binding)
        await self._ctx.route("**/*", self._route)
        self._page = await self._ctx.new_page()
        self._page.on("dialog", self._dialog)
        self._page.on("request", self._on_request)
        self._page.on("requestfinished", self._on_request_done)
        self._page.on("requestfailed", self._on_request_done)
        self._page.on("framenavigated", self._on_navigated)
        self._page.on("close", self._on_close)

    async def close(self) -> Path | None:
        """Close the session; returns the video path if one was recorded."""
        video: Path | None = None
        try:
            if self._page is not None and self._page.video is not None and not self.closed:
                page_video = self._page.video
                if self._ctx:
                    await self._ctx.close()
                video = Path(await page_video.path())
            elif self._ctx:
                await self._ctx.close()
        except PWError:
            pass
        finally:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
            self.closed = True
        return video

    async def bring_to_front(self) -> None:
        await self.page.bring_to_front()

    # ------------------------------------------------------------------ event plumbing
    def _on_request(self, req: Request) -> None:
        if req.resource_type == "document":
            self._inflight_docs += 1

    def _on_request_done(self, req: Request) -> None:
        if req.resource_type == "document":
            self._inflight_docs = max(0, self._inflight_docs - 1)

    def _on_navigated(self, _frame: Frame) -> None:
        self._last_nav = time.monotonic()

    def _on_close(self, _page: Page) -> None:
        self.closed = True

    async def _dialog(self, dialog: Dialog) -> None:
        if self.on_dialog:
            await self.on_dialog(dialog)
        else:  # safe default
            await dialog.dismiss()

    def _capture_binding(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.on_capture:
            frame = source.get("frame")
            container = self.container_of(frame) if isinstance(frame, Frame) else []
            self.on_capture(payload, container)

    async def _route(self, route: Route) -> None:
        req = route.request
        allowed = self.route_guard(req.url, req.resource_type) if self.route_guard else True
        if allowed:
            await route.continue_()
        else:
            self.blocked_requests.append({"url": req.url, "type": req.resource_type})
            await route.abort("blockedbyclient")

    def settled(self, quiet_ms: int) -> bool:
        return self._inflight_docs == 0 and (time.monotonic() - self._last_nav) * 1000 >= quiet_ms

    # ------------------------------------------------------------------ frames
    @staticmethod
    def container_of(frame: Frame) -> list[str]:
        path: list[str] = []
        f: Frame | None = frame
        while f is not None and f.parent_frame is not None:
            path.insert(0, f"frame:{f.name}")
            f = f.parent_frame
        return path

    def frame_for(self, container: list[str]) -> Frame | None:
        if self.closed:
            raise SessionLost("page closed")
        want = list(container)
        for f in self.page.frames:
            if not f.is_detached() and self.container_of(f) == want:
                return f
        return None

    async def _eval(self, frame: Frame, expr: str, arg: Any = None) -> Any:
        try:
            return await frame.evaluate(expr, arg)
        except PWError as e:
            raise _classify(e) from e

    async def goto(self, route: str) -> None:
        try:
            await self.page.goto(self.base_url + route, wait_until="domcontentloaded")
        except PWError as e:
            raise _classify(e) from e

    def url_path(self, container: list[str]) -> str | None:
        f = self.frame_for(container)
        return urlparse(f.url).path if f else None

    # ------------------------------------------------------------------ perception
    async def snapshot(self) -> PageSnapshot:
        frames: list[FrameSnapshot] = []
        ref_index: dict[str, tuple[tuple[str, ...], str]] = {}
        start = 0
        for f in list(self.page.frames):
            if f.is_detached():
                continue
            container = self.container_of(f)
            try:
                snap = await self._eval(
                    f, "(o) => window.__cu ? window.__cu.snapshot(o) : null", {"start": start}
                )
            except NotReady:
                continue
            if not snap:
                continue
            start = snap["next"]
            fs = FrameSnapshot(
                container=container,
                url_path=urlparse(snap["url"]).path,
                title=snap["title"],
                doc_id=snap["doc_id"],
                frameset=snap["frameset"],
                items=snap["items"],
                offset=await self._frame_offset(f),
            )
            frames.append(fs)
            for item in fs.items:
                for ref in iter_refs(item):
                    ref_index[ref] = (tuple(container), fs.doc_id)
        return PageSnapshot(frames=frames, ref_index=ref_index)

    async def _frame_offset(self, f: Frame) -> tuple[float, float]:
        if f.parent_frame is None:
            return (0.0, 0.0)
        try:
            box = await (await f.frame_element()).bounding_box()
        except PWError:
            return (0.0, 0.0)
        return (box["x"], box["y"]) if box else (0.0, 0.0)

    async def page_text(self, container: list[str]) -> str | None:
        f = self.frame_for(container)
        if f is None:
            return None
        return await self._eval(f, "() => window.__cu ? window.__cu.pageText() : null")

    async def red_texts(self, container: list[str]) -> list[str]:
        f = self.frame_for(container)
        if f is None:
            return []
        return await self._eval(f, "() => window.__cu ? window.__cu.redTexts() : []") or []

    async def doc_id(self, container: list[str]) -> str | None:
        f = self.frame_for(container)
        if f is None:
            return None
        return await self._eval(f, "() => window.__cu ? window.__cu.docId() : null")

    async def page_states(self) -> dict[tuple[str, ...], dict[str, Any]]:
        """Per-frame identity (doc nonce, path, emphasized texts, text) without resetting refs."""
        out: dict[tuple[str, ...], dict[str, Any]] = {}
        for f in list(self.page.frames):
            if f.is_detached():
                continue
            try:
                st = await self._eval(f, "() => window.__cu ? window.__cu.state() : null")
            except NotReady:
                continue
            if st and not st["frameset"]:
                st["url_path"] = urlparse(st["url"]).path
                out[tuple(self.container_of(f))] = st
        return out

    async def doc_ids(self) -> dict[tuple[str, ...], str]:
        out: dict[tuple[str, ...], str] = {}
        for f in list(self.page.frames):
            if f.is_detached():
                continue
            try:
                d = await self._eval(f, "() => window.__cu ? window.__cu.docId() : null")
            except NotReady:
                continue
            if d:
                out[tuple(self.container_of(f))] = d
        return out

    # ------------------------------------------------------------------ targeting
    async def element_for_ref(self, snap: PageSnapshot, ref: str) -> tuple[Frame, ElementHandle]:
        entry = snap.ref_index.get(ref)
        if entry is None:
            raise StaleRef(f"unknown ref {ref}")
        container, doc_id = entry
        f = self.frame_for(list(container))
        if f is None or await self._eval(f, "() => window.__cu.docId()") != doc_id:
            raise StaleRef(f"ref {ref} belongs to a page that is no longer loaded; observe again")
        try:
            handle = await f.evaluate_handle("(r) => window.__cu.byRef(r)", ref)
        except PWError as e:
            raise _classify(e) from e
        el = handle.as_element()
        if el is None:
            raise StaleRef(f"ref {ref} no longer exists")
        return f, el

    async def resolve(
        self, container: list[str], strategies: list[dict[str, Any]]
    ) -> tuple[Resolution | None, list[tuple[dict[str, Any], int]], bool]:
        """Try strategies in order; first one with exactly one match wins.

        Returns (resolution, diagnostics, container_present).
        """
        f = self.frame_for(container)
        if f is None:
            return None, [], False
        diagnostics: list[tuple[dict[str, Any], int]] = []
        for i, s in enumerate(strategies):
            n = await self._eval(f, "(s) => window.__cu.count(s)", s)
            diagnostics.append((s, n))
            if n == 1:
                try:
                    handle = await f.evaluate_handle("(s) => window.__cu.resolve(s)[0]", s)
                except PWError as e:
                    raise _classify(e) from e
                el = handle.as_element()
                if el is not None:
                    return Resolution(f, el, i, s, diagnostics), diagnostics, True
        return None, diagnostics, True

    async def strategies_agree(self, container: list[str], a: dict[str, Any], b: dict[str, Any]) -> bool:
        f = self.frame_for(container)
        if f is None:
            return False
        return bool(
            await self._eval(
                f,
                "([a, b]) => { const x = window.__cu.resolve(a), y = window.__cu.resolve(b);"
                " return x.length === 1 && y.length === 1 && x[0] === y[0]; }",
                [a, b],
            )
        )

    async def near_misses(self, container: list[str], strategy: dict[str, Any]) -> list[dict[str, Any]]:
        f = self.frame_for(container)
        if f is None:
            return []
        try:
            return await self._eval(f, "(s) => window.__cu.nearMisses(s)", strategy) or []
        except (NotReady, PWError):
            return []

    @staticmethod
    async def describe(el: ElementHandle) -> dict[str, Any]:
        try:
            return await el.evaluate("(el) => window.__cu.describe(el)")
        except PWError as e:
            raise _classify(e) from e

    @staticmethod
    async def read_text(el: ElementHandle) -> str:
        try:
            return await el.evaluate("(el) => window.__cu.readText(el)")
        except PWError as e:
            raise _classify(e) from e

    @staticmethod
    async def read_table(el: ElementHandle, columns: dict[str, str]) -> list[dict[str, str | None]]:
        try:
            return await el.evaluate("(el, cols) => window.__cu.readTable(el, cols)", columns)
        except PWError as e:
            raise _classify(e) from e

    # ------------------------------------------------------------------ actions (real input events)
    async def click(self, el: ElementHandle, timeout_ms: int = 3000) -> None:
        try:
            await el.click(timeout=timeout_ms)
        except PWError as e:
            raise _classify(e) from e
        finally:
            self._last_nav = time.monotonic()  # an action may start a navigation a few ms later

    async def fill(self, el: ElementHandle, value: str, timeout_ms: int = 3000) -> None:
        try:
            await el.fill(value, timeout=timeout_ms)
        except PWError as e:
            raise _classify(e) from e
        finally:
            self._last_nav = time.monotonic()  # an action may start a navigation a few ms later

    async def select(self, el: ElementHandle, label: str, timeout_ms: int = 3000) -> None:
        try:
            await el.select_option(label=label, timeout=timeout_ms)
        except PWError as e:
            raise _classify(e) from e
        finally:
            self._last_nav = time.monotonic()  # an action may start a navigation a few ms later

    async def press(self, el: ElementHandle, key: str, timeout_ms: int = 3000) -> None:
        try:
            await el.press(key, timeout=timeout_ms)
        except PWError as e:
            raise _classify(e) from e
        finally:
            self._last_nav = time.monotonic()  # an action may start a navigation a few ms later

    # ------------------------------------------------------------------ evidence
    async def screenshot(
        self,
        sensitive_labels: list[str],
        marks: list[tuple[str, list[float]]] | None = None,
        sensitive_values: list[str] | None = None,
    ) -> bytes | None:
        """Masked screenshot. Returns None rather than risk an unmasked image.

        Two layers: CSS paints over whole elements marked sensitive (label-based PII,
        money cells, secret inputs), and exact substring rectangles are painted over for
        values that share a text node with other text (secrets, identifying inputs).
        """
        boxes: list[list[float]] = []
        for attempt in range(2):
            failed = False
            boxes = []
            for f in list(self.page.frames):
                if f.is_detached():
                    continue
                try:
                    await self._eval(
                        f, "(l) => window.__cu ? window.__cu.markSensitive(l) : 0", sensitive_labels
                    )
                    if sensitive_values:
                        rects = await self._eval(
                            f, "(v) => window.__cu ? window.__cu.valueRects(v) : []", sensitive_values
                        )
                        ox, oy = await self._frame_offset(f)
                        boxes.extend([x + ox, y + oy, w, h] for x, y, w, h in rects or [])
                except NotReady:
                    failed = True
            if not failed:
                break
            if attempt == 1:
                return None
            await self.page.wait_for_timeout(250)
        try:
            png = await self.page.screenshot(style=MASK_CSS, animations="disabled", caret="hide")
        except PWError as e:
            err = _classify(e)
            if isinstance(err, SessionLost):
                raise err from e
            return None
        if boxes:
            png = _paint(png, boxes)
        return _draw_marks(png, marks) if marks else png


def _paint(png: bytes, boxes: list[list[float]]) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for x, y, w, h in boxes:
        draw.rectangle([x, y, x + w, y + h], fill=(47, 47, 47))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _draw_marks(png: bytes, marks: list[tuple[str, list[float]]]) -> bytes:
    """Set-of-marks: outline each actionable element and tag it with its ref."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:  # pragma: no cover - older Pillow
        font = ImageFont.load_default()
    for ref, (x, y, w, h) in marks:
        if w <= 0 or h <= 0:
            continue
        draw.rectangle([x, y, x + w, y + h], outline=(220, 38, 38), width=1)
        tw = draw.textlength(ref, font=font)
        draw.rectangle([x, y - 12, x + tw + 4, y], fill=(220, 38, 38))
        draw.text((x + 2, y - 12), ref, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
