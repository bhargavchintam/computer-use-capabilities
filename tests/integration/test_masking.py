"""Screenshot masking holds on high-density displays.

Values that share a text node with other text ("Operator: teller01") are painted over using
rectangles in CSS pixels. A headed window on a 2x (Retina) screen produces 2x screenshots, which
once put those boxes in the wrong place. Screenshots are now taken at CSS scale; this test
emulates a 2x display and checks the paint lands on the value.
"""

from __future__ import annotations

import asyncio
import io
import os

import pytest
from PIL import Image

from cua.surface.base import NotReady
from cua.surface.web import WebSurface
from tests.conftest import Bank

pytestmark = pytest.mark.integration
PAINT = (47, 47, 47)


async def test_painted_masks_land_on_the_values_on_a_2x_display(bank: Bank) -> None:
    operator = os.environ["PINECREST_OPERATOR_ID"]
    surface = WebSurface(bank.url("pinecrest"), headed=False, device_scale_factor=2)
    await surface.start()
    try:
        await surface.goto("/signon")
        for label, value in (
            ("Operator ID:", operator),
            ("Password:", os.environ["PINECREST_OPERATOR_PASSWORD"]),
        ):
            box, _, _ = await surface.resolve([], [{"kind": "label", "role": "textbox", "label": label}])
            assert box is not None
            await surface.fill(box.element, value)
        button, _, _ = await surface.resolve([], [{"kind": "role_name", "role": "button", "name": "Sign On"}])
        assert button is not None
        await surface.click(button.element)
        for _ in range(100):  # the banner frame shows "Operator: <id>" once signed on
            try:
                if operator.lower() in (await surface.page_text(["frame:banner"]) or "").lower():
                    break
            except NotReady:
                pass
            await asyncio.sleep(0.1)
        png = await surface.screenshot([], sensitive_values=[operator])
        banner = surface.frame_for(["frame:banner"])
        assert png is not None and banner is not None
        rects = await banner.evaluate("(v) => window.__cu.valueRects(v)", [operator])
        ox, oy = await surface._frame_offset(banner)
    finally:
        await surface.close()
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.size == (1280, 860), "the screenshot is in CSS pixels, like the paint boxes"
    assert rects, "the operator id is on the page"
    x, y, w, h = rects[0]
    assert img.getpixel((int(ox + x + w / 2), int(oy + y + h / 2))) == PAINT
