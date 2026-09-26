"""Oracle check: our in-page accessible names agree with Playwright's ARIA engine.

Targeting uses our own resolver (cu_bundle.js), so record-time validation and replay can never
disagree and the same logic can serve surfaces Playwright does not cover. That makes Playwright's
get_by_role an independent oracle: on every page of the two write/read flows, on both tenants,
every element we would target by role + accessible name must be exactly the element Playwright
finds with get_by_role(role, name=..., exact=True). Unlabeled legacy inputs have no accessible
name at all, which is why our proximity labels exist; Playwright cannot see those, so they are
driven here through our own label strategy.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from playwright.async_api import Frame, Page, async_playwright, expect

from cua.surface.web import BUNDLE
from tests.conftest import Bank

pytestmark = pytest.mark.integration

ARIA_ROLES = ["button", "checkbox", "combobox", "heading", "img", "link", "listbox", "radio", "textbox"]

# Tag every visible element we would target by a unique role+name strategy, so Playwright's
# answer can be compared by identity, not by text.
COLLECT = """(roles) => {
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll('*')) {
    if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
    const d = window.__cu.describe(el);
    if (!d || !roles.includes(d.role)) continue;
    const c = d.candidates.find((x) => x.strategy.kind === 'role_name' && x.unique);
    if (!c) continue;
    el.setAttribute('data-oracle', String(i));
    out.push({ id: String(i++), role: d.role, name: c.strategy.name });
  }
  return out;
}"""


async def oracle(page: Page) -> tuple[list[str], int]:
    problems: list[str] = []
    checked = 0
    for frame in page.frames:
        if frame.is_detached() or not await frame.evaluate("() => !!window.__cu"):
            continue
        for item in await frame.evaluate(COLLECT, ARIA_ROLES):
            where = f"[{frame.name or 'top'}] {item['role']} {item['name']!r}"
            found = frame.get_by_role(item["role"], name=item["name"], exact=True)
            n = await found.count()
            if n != 1:
                problems.append(f"{where}: Playwright finds {n} elements")
            elif (got := await found.get_attribute("data-oracle")) != item["id"]:
                problems.append(f"{where}: Playwright finds a different element ({got})")
            else:
                checked += 1
    return problems, checked


async def fill_by_label(frame: Frame, label: str, value: str) -> None:
    handle = await frame.evaluate_handle(
        "(s) => window.__cu.resolve(s)[0]", {"kind": "label", "role": "textbox", "label": label}
    )
    el = handle.as_element()
    assert el is not None, f"no textbox labelled {label!r}"
    await el.fill(value)


def work(page: Page) -> Frame:
    frame = page.frame(name="work")
    assert frame is not None
    return frame


@pytest.mark.parametrize(
    ("tenant", "member", "menu_link", "search", "share_type"),
    [
        ("pinecrest", "10042", "Member Inquiry", "Search", "Holiday Club"),
        ("lakeside", "20064", "Member Lookup", "Find", "Christmas Club"),
    ],
)
async def test_resolver_names_agree_with_playwright(
    bank: Bank, tenant: str, member: str, menu_link: str, search: str, share_type: str
) -> None:
    report: dict[str, Any] = {}

    async def check(page_name: str) -> None:
        report[page_name] = await oracle(page)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context()
        await ctx.add_init_script(script=BUNDLE)
        page = await ctx.new_page()
        await page.goto(bank.url(tenant) + "/signon")
        await check("sign-on")

        await fill_by_label(page.main_frame, "Operator ID:", os.environ[f"{tenant.upper()}_OPERATOR_ID"])
        await fill_by_label(page.main_frame, "Password:", os.environ[f"{tenant.upper()}_OPERATOR_PASSWORD"])
        await page.get_by_role("button", name="Sign On", exact=True).click()
        await page.wait_for_url("**/core/main")
        await page.wait_for_function("() => window.frames.length >= 3")
        await expect(work(page).locator("body")).to_contain_text("Welcome")
        await check("main menu (frameset)")

        nav = page.frame(name="nav")
        assert nav is not None
        await nav.get_by_role("link", name=menu_link, exact=True).click()
        await expect(work(page).get_by_role("button", name=search, exact=True)).to_be_visible()
        await check("inquiry form")

        await fill_by_label(work(page), "Member #:", member)
        await work(page).get_by_role("button", name=search, exact=True).click()
        await expect(work(page).locator("body")).to_contain_text("Member Summary")
        await check("member summary")

        await work(page).get_by_role("link", name="Open New Share", exact=True).click()
        await expect(work(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
        await check("new share form")

        select = await work(page).evaluate_handle(
            "(s) => window.__cu.resolve(s)[0]", {"kind": "label", "role": "combobox", "label": "Share Type:"}
        )
        el = select.as_element()
        assert el is not None
        await el.select_option(label=share_type)
        await fill_by_label(work(page), "Initial Deposit:", "7500.00")
        await fill_by_label(work(page), "Nickname:", "Oracle check")
        await work(page).get_by_role("button", name="Continue", exact=True).click()
        await expect(work(page).locator("body")).to_contain_text("Review New Share")
        await check("review")

        await work(page).get_by_role("button", name="Confirm", exact=True).click()
        await expect(work(page).locator("body")).to_contain_text("Supervisor Override Required")
        await check("supervisor override")

        await fill_by_label(work(page), "Supervisor PIN:", os.environ["MOCK_SUPERVISOR_PIN"])
        await work(page).get_by_role("button", name="Approve", exact=True).click()
        await expect(work(page).locator("body")).to_contain_text("Share Opened")
        await check("receipt")
        await browser.close()

    problems = [f"{name}: {p}" for name, (ps, _) in report.items() for p in ps]
    assert not problems, "\n".join(problems)
    counts = {name: n for name, (_, n) in report.items()}
    assert all(n > 0 for n in counts.values()), counts  # every page had targets to compare
    assert sum(counts.values()) >= 30, counts
