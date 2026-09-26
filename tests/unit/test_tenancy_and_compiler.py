from __future__ import annotations

import pytest

from cua.agent.loop import _map_columns
from cua.compiler import _landmark, _parameterize, lint, prune
from cua.configio import load_app_profile, load_overlays, load_policy, load_tenant
from cua.tenancy import resolve_plan, version_in
from cua.trace import FrameState, PageState, TraceStep
from tests.conftest import load_fixture


def plan(tenant: str, use_overlays: bool = True):  # type: ignore[no-untyped-def]
    cap = load_fixture("acmecore.member.get_share_balance")
    return resolve_plan(
        cap,
        load_tenant(tenant),
        load_app_profile("acmecore"),
        load_policy("default"),
        load_overlays(),
        use_overlays=use_overlays,
    )


def test_version_ranges() -> None:
    assert version_in(">=7.3,<7.4", "7.3.1") and not version_in(">=7.3,<7.4", "7.2.4")


def test_overlay_applies_only_to_its_vendor_version() -> None:
    assert plan("pinecrest").overlays_applied == []
    p = plan("lakeside")
    assert p.overlays_applied == ["acmecore-7.3"]
    first = p.capability.step("click_member_inquiry").target.strategies[0].model_dump()
    assert first == {"kind": "role_name", "role": "link", "name": "Member Lookup"}
    assert p.labels == {"share_type": {"Holiday Club": "Christmas Club"}}
    # the contract is untouched by overlays
    assert p.capability.contract == p.base.contract


def test_plan_hash_covers_overlays() -> None:
    assert plan("lakeside").plan_sha256 != plan("lakeside", use_overlays=False).plan_sha256


def test_overlay_for_unknown_step_is_rejected() -> None:
    cap = load_fixture("acmecore.member.get_share_balance")
    ov = load_overlays()[0]
    bad = ov.model_copy(deep=True)
    bad.capabilities[cap.id].steps["no_such_step"] = bad.capabilities[cap.id].steps.pop(
        "click_member_inquiry"
    )
    with pytest.raises(ValueError, match="unknown step"):
        resolve_plan(
            cap, load_tenant("lakeside"), load_app_profile("acmecore"), load_policy("default"), [bad]
        )


def test_map_columns() -> None:
    assert _map_columns(
        ["share_id", "description", "balance"], ["Description", "Share ID", "Rate", "Balance"]
    ) == {"share_id": "Share ID", "description": "Description", "balance": "Balance"}
    assert _map_columns(["iban"], ["Share ID", "Balance"]) is None


def test_parameterize_row_keys_and_drop_data_strategies() -> None:
    samples = {"member_number": "10042"}
    cell = {
        "kind": "table_cell",
        "headers": ["Member #", "Name"],
        "row": {"column": "Member #", "equals": "10042"},
        "column": "Name",
    }
    assert _parameterize(cell, samples)["row"]["equals"] == {"param": "member_number"}  # type: ignore[index]
    assert _parameterize({"kind": "role_name", "role": "link", "name": "10042"}, samples) is None
    keep = {"kind": "role_name", "role": "link", "name": "Member Inquiry"}
    assert _parameterize(keep, samples) == keep


def _state(path: str, doc: str, title: str) -> PageState:
    return PageState({("frame:work",): FrameState(("frame:work",), path, doc, [title], title)})


def test_prune_removes_detours_that_return_to_the_same_screen() -> None:
    home, reports = _state("/core/welcome", "d1", "Welcome"), _state("/core/reports", "d2", "Not Authorized")
    home2, inquiry = _state("/core/welcome", "d3", "Welcome"), _state("/core/inquiry", "d4", "Member Inquiry")
    steps = [
        TraceStep(0, "agent", "click", ["frame:nav"], {}, before=home, after=reports),
        TraceStep(1, "agent", "click", ["frame:nav"], {}, before=reports, after=home2),
        TraceStep(2, "agent", "click", ["frame:nav"], {}, before=home2, after=inquiry),
    ]
    kept, notes = prune(steps)
    assert [s.index for s in kept] == [2] and notes


def test_landmarks_skip_data_and_numbers() -> None:
    before = FrameState(("frame:work",), "/core/inquiry", "d1", ["Member Inquiry"], "")
    after = FrameState(("frame:work",), "/core/inquiry", "d2", ["HARTWELL, JUNE M", "Member Summary"], "")
    assert _landmark(before, after, avoid=["HARTWELL, JUNE M"]) == "Member Summary"
    after2 = FrameState(("frame:work",), "/core/x", "d3", ["Confirmation # CN7K2Q9D4X"], "")
    assert _landmark(None, after2, avoid=[]) is None


def test_linter_fails_closed_on_leaks() -> None:
    cap = load_fixture("acmecore.member.get_share_balance")
    assert lint(cap, samples={"member_number": "10042"}, secrets=["pc-Teller-9x41"]) == []
    data = cap.model_dump(mode="json", exclude_none=True)
    data["implementation"]["steps"][2]["intent"] = "Search for 10042 whose balance is $12,450.31"
    leaky = type(cap).model_validate(data)
    findings = lint(leaky, samples={"member_number": "10042"}, secrets=[])
    assert any("member_number" in f for f in findings) and any("money" in f for f in findings)
