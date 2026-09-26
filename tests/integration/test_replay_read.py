"""Replay of the read capability against the live mock: every runtime condition, classified."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.models import Capability
from cua.replay import run_replay
from tests.conftest import Bank, approved

pytestmark = pytest.mark.integration


async def test_happy_path_is_deterministic(bank: Bank, balance_cap: Capability, tmp_path: Path) -> None:
    first = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    second = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert first.status == second.status == "succeeded"
    assert first.side_effect == "none" and first.retry_safe
    assert first.outputs is not None
    assert first.outputs["savings_balance"] == {"amount": "12450.31", "currency": "USD"}
    assert [r["share_id"] for r in first.outputs["shares"]] == ["S01", "S10", "S50"]
    assert all(s.strategy and s.strategy.endswith("#0") for s in first.steps), "primary strategies only"
    assert first.trace_sha256 == second.trace_sha256
    assert first.outputs == second.outputs


async def test_member_not_found_is_a_business_outcome(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "99999"}, runs_root=tmp_path
    )
    assert r.status == "business_outcome"
    assert r.outcome and r.outcome.code == "MEMBER_NOT_FOUND" and r.outcome.caller_guidance
    assert r.error is None and r.outputs is None


async def test_restricted_account_is_a_business_outcome(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10013"}, runs_root=tmp_path
    )
    assert r.status == "business_outcome" and r.outcome and r.outcome.code == "ACCOUNT_RESTRICTED"


async def test_invalid_input_is_rejected_before_touching_the_ui(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "12AB5"}, runs_root=tmp_path
    )
    assert r.status == "rejected" and r.error and r.error.code == "INPUT_INVALID"
    assert "12AB5" not in r.error.message  # never echo the value
    events = (Path(r.evidence_dir or "") / "events.jsonl").read_text()
    assert "session_started" not in events


async def test_maintenance_overlay_is_recovered(bank: Bank, balance_cap: Capability, tmp_path: Path) -> None:
    bank.fault("pinecrest", "maintenance", path="/core/inquiry")
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10077"}, runs_root=tmp_path
    )
    assert r.status == "succeeded"
    assert [x.detector for x in r.recoveries] == ["maintenance_notice"]


async def test_informational_alert_is_recovered(bank: Bank, balance_cap: Capability, tmp_path: Path) -> None:
    bank.fault("pinecrest", "alert", path="/core/inquiry", message="Your password will expire in 3 days.")
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "succeeded"
    assert any(x.detector.startswith("dialog:") for x in r.recoveries)


async def test_unknown_dialog_fails_safe(bank: Bank, balance_cap: Capability, tmp_path: Path) -> None:
    bank.fault(
        "pinecrest", "alert", path="/core/inquiry", message="Posting batch 7 is locked by another user."
    )
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "failed" and r.error and r.error.code == "UNRECOGNIZED_STATE"
    assert r.error.evidence, "failure carries a screenshot and a snapshot"


async def test_session_expiry_is_recovered_by_reauth_and_redrive(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "expire", path="/core/inquiry")
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "succeeded", r.error
    assert [x.detector for x in r.recoveries] == ["session_expired", "session_expired"]
    assert [x.action for x in r.recoveries] == ["signed on again", "re-driving the flow from the entry route"]


async def test_transient_server_error_is_recovered(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "error500", path="/core/inquiry", count=1)
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "succeeded" and [x.detector for x in r.recoveries] == ["server_error"]


async def test_persistent_server_error_is_a_retryable_hard_failure(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "error500", path="/core/inquiry", count=10)
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "failed" and r.error and r.error.code == "APP_ERROR" and r.error.retryable
    assert len(r.recoveries) == 2  # bounded
    assert r.side_effect == "none"


async def test_slow_page_within_budget_just_waits(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    bank.fault("pinecrest", "slow", path="/core/inquiry", delay_ms=3000, count=2)
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    assert r.status == "succeeded" and not r.recoveries


async def test_draft_is_rejected_on_a_production_tenant(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        balance_cap, tenant_id="lakeside", inputs={"member_number": "20031"}, runs_root=tmp_path
    )
    assert r.status == "rejected" and r.error and r.error.code == "NOT_APPROVED"


async def test_edited_after_approval_is_rejected(bank: Bank, balance_cap: Capability, tmp_path: Path) -> None:
    cap = approved(balance_cap)
    edited = cap.model_copy(update={"title": cap.title + " (edited)"})
    r = await run_replay(edited, tenant_id="lakeside", inputs={"member_number": "20031"}, runs_root=tmp_path)
    assert r.status == "rejected" and r.error and "edited after approval" in r.error.message


async def test_version_drift_without_overlay_fails_with_near_miss(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        approved(balance_cap),
        tenant_id="lakeside",
        inputs={"member_number": "20031"},
        runs_root=tmp_path,
        use_overlays=False,
    )
    assert r.status == "failed" and r.error and r.error.code == "TARGET_NOT_FOUND"
    assert r.error.step_id == "click_member_inquiry"
    assert any("Member Lookup" in m for m in r.error.near_misses)
    assert r.error.hint and "overlay" in r.error.hint


async def test_version_overlay_generalizes_with_drift_warning(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        approved(balance_cap), tenant_id="lakeside", inputs={"member_number": "20031"}, runs_root=tmp_path
    )
    assert r.status == "succeeded", r.error
    assert r.overlays_applied == ["acmecore-7.3"]
    assert (
        r.outputs and r.outputs["savings_balance"]["amount"] == "5620.18"
    )  # reordered columns still read correctly
    assert [w.code for w in r.warnings] == ["FALLBACK_LOCATOR_USED"]
    assert next(s for s in r.steps if s.step_id == "click_search").strategy == "attr#1"


async def test_results_on_disk_never_contain_raw_outputs(
    bank: Bank, balance_cap: Capability, tmp_path: Path
) -> None:
    r = await run_replay(
        balance_cap, tenant_id="pinecrest", inputs={"member_number": "10042"}, runs_root=tmp_path
    )
    run_dir = Path(r.evidence_dir or "")
    blob = "".join(p.read_text() for p in run_dir.rglob("*") if p.suffix in (".json", ".jsonl", ".txt"))
    for secret in ("12450.31", "12,450.31", "10042", "HARTWELL", "900-12-4417", "4417"):
        assert secret not in blob, secret
