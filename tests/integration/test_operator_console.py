"""The operator console, used the way a person uses it: a real browser on the console page.

Every decision (approve, take control, hand back) goes through the console UI. The run itself is
headless here because CI has no display; the one thing a headless run cannot give a person is the
bank window, so the "hands in the same live session" part is played by a hook acting on the very
session the replay is driving. The headed end-to-end version is in evidence/ (a real person).
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright, expect

from cua.control import ControlState, SessionController
from cua.models import Capability
from cua.replay import run_replay
from cua.runtime import RuntimeSession
from tests.conftest import Bank

pytestmark = pytest.mark.integration
BASE = {"member_number": "10042", "share_type": "Holiday Club"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def open_console(page: Page, port: int, operator: str) -> None:
    for _ in range(300):  # the runner starts the console after its browser session is up
        try:
            await page.goto(f"http://127.0.0.1:{port}/")
            break
        except Exception:
            await asyncio.sleep(0.1)
    await expect(page.locator("#run")).to_contain_text("Run ")
    await page.fill("#op", operator)


async def test_console_approval_commits_with_the_operator_recorded(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    port = _free_port()
    seen: dict[str, object] = {}

    async def operator_in_browser() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await open_console(page, port, "console-tester")
            approve = page.get_by_role("button", name="Approve step")
            await approve.wait_for(timeout=60_000)
            seen["lease"] = await page.locator("#lease").inner_text()
            img = page.get_by_role("img", name="masked screenshot at escalation")
            await expect(img).to_be_visible()
            seen["screenshot_loaded"] = await img.evaluate("(i) => i.complete && i.naturalWidth > 0")
            seen["proposed"] = await page.locator("dl").first.inner_text()
            await approve.click()
            await browser.close()

    ui = asyncio.create_task(operator_in_browser())
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "250.00", "nickname": "Gift fund"},
        operator="console",
        headed=False,
        console_port=port,
        runs_root=tmp_path,
    )
    await asyncio.wait_for(ui, timeout=30)
    assert r.status == "succeeded", r.error
    assert r.side_effect == "committed" and len(bank.state("pinecrest")["receipts"]) == 1
    iv = r.interventions[0]
    assert iv.kind == "approval" and iv.decision == "approve" and iv.operator == "console-tester"
    assert "awaiting human" in str(seen["lease"]) and seen["screenshot_loaded"] is True
    assert "click_confirm" in str(seen["proposed"])
    events = (Path(r.evidence_dir or "") / "events.jsonl").read_text()
    assert '"type": "operator_decision_received"' in events and '"operator": "console-tester"' in events


async def test_console_take_control_and_hand_back_on_the_same_session(
    bank: Bank, share_cap: Capability, tmp_path: Path
) -> None:
    port = _free_port()
    pin = os.environ["MOCK_SUPERVISOR_PIN"]
    human_done = asyncio.Event()
    seen: dict[str, str] = {}

    async def hands_in_the_bank_window(controller: SessionController, session: RuntimeSession) -> None:
        while controller.state != ControlState.HUMAN:  # control is granted from the console below
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.2)  # like a person reading the screen before typing
        s = session.surface
        box, _, _ = await s.resolve(
            ["frame:work"], [{"kind": "label", "role": "textbox", "label": "Supervisor PIN:"}]
        )
        assert box is not None
        await box.element.click()
        await box.element.type(pin, delay=30)
        ok, _, _ = await s.resolve(
            ["frame:work"], [{"kind": "role_name", "role": "button", "name": "Approve"}]
        )
        assert ok is not None
        await ok.element.click()
        human_done.set()

    async def operator_in_browser() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await open_console(page, port, "supervisor-jo")
            take = page.get_by_role("button", name="Take control of the live session")
            await take.wait_for(timeout=90_000)
            await take.click()
            await expect(page.locator("#lease")).to_contain_text("human · holder: supervisor-jo")
            await human_done.wait()
            actions = page.locator("pre").first
            await expect(actions).to_contain_text('button "Approve"')
            await asyncio.sleep(2.5)  # let the 1.2 s poll settle so the note box is not re-rendered
            seen["actions"] = await actions.inner_text()
            seen["page"] = await page.content()
            seen["api"] = await (await page.request.get(f"http://127.0.0.1:{port}/api/state")).text()
            # the console tells the operator the step is done, so they hand back on the result page
            await expect(page.locator("#lease")).to_contain_text("STEP COMPLETE")
            await page.get_by_placeholder("What did you do?").fill("Entered the supervisor PIN and approved")
            await page.get_by_role("button", name="Hand control back to automation").click()
            await browser.close()

    ui = asyncio.create_task(operator_in_browser())
    r = await run_replay(
        share_cap,
        tenant_id="pinecrest",
        inputs={**BASE, "initial_deposit": "7500.00", "nickname": "Trip fund"},
        approve=True,
        operator="console",
        operator_hook=hands_in_the_bank_window,
        headed=False,
        console_port=port,
        runs_root=tmp_path,
    )
    await asyncio.wait_for(ui, timeout=30)
    assert r.status == "succeeded", r.error
    assert r.side_effect == "committed"
    assert next(s for s in r.steps if s.step_id == "click_confirm").status == "completed_by_human"
    iv = r.interventions[0]
    assert iv.kind == "human_required" and iv.decision == "hand_back" and iv.operator == "supervisor-jo"
    assert iv.note == "Entered the supervisor PIN and approved"
    assert 'textbox "Supervisor PIN:"' in seen["actions"] and 'button "Approve"' in seen["actions"]
    for where in ("actions", "page", "api"):
        assert pin not in seen[where], f"PIN visible in the console {where}"
    assert '"step_done": true' in seen["api"].replace("\n", " ") or '"step_done":true' in seen["api"]
    receipts = bank.state("pinecrest")["receipts"]
    assert len(receipts) == 1 and receipts[0]["supervisor"] is True
    for p in Path(r.evidence_dir or "").rglob("*"):
        if p.is_file() and p.suffix != ".png":
            assert pin not in p.read_text(errors="ignore"), f"PIN leaked into {p}"
