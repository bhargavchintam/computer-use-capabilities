"""The agent-facing catalog: tools are generated from capability contracts, and only approved,
version-compatible capabilities are offered to a tenant."""

from __future__ import annotations

from pathlib import Path

from cua.catalog import caller_view, eligible, tool_for, tool_name
from cua.models import Capability, FailureInfo, RunResult
from cua.registry import Registry
from tests.conftest import approved, load_fixture

READ, WRITE = "acmecore.member.get_share_balance", "acmecore.member.open_share"


def _with(cap: Capability, **impl_app: str) -> Capability:
    data = cap.model_dump(mode="json", exclude_none=True)
    data["implementation"]["app"].update(impl_app)
    return Capability.model_validate(data)


def test_tool_is_generated_from_the_contract() -> None:
    tool = tool_for(approved(load_fixture(READ)))
    assert tool["name"] == "acmecore__member__get_share_balance" == tool_name(READ)
    assert tool["strict"] is True
    schema = tool["input_schema"]
    assert schema["required"] == ["member_number"] and schema["additionalProperties"] is False
    assert "^[0-9]{5}$" in schema["properties"]["member_number"]["description"]
    desc = tool["description"]
    assert "Read-only and safe to retry" in desc
    for code in ("MEMBER_NOT_FOUND", "ACCOUNT_RESTRICTED", "INPUT_REJECTED_BY_APP"):
        assert code in desc  # the calling agent learns the business outcomes up front
    assert "savings_balance (money)" in desc and "shares (table)" in desc


def test_commit_capabilities_say_they_need_a_human() -> None:
    desc = tool_for(approved(load_fixture(WRITE)))["description"]
    assert "COMMITS a change and needs a human approval" in desc


def test_only_approved_compatible_latest_versions_are_offered(tmp_path: Path) -> None:
    reg = Registry(tmp_path)
    read = approved(load_fixture(READ))
    reg.save(read)
    reg.save(load_fixture(WRITE))  # draft: never offered
    newer = approved(_with(read.model_copy(update={"version": "1.1.0"}), versions=">=7.2,<7.3"))
    reg.save(newer)  # approved, but only for 7.2
    edited = approved(read.model_copy(update={"version": "1.2.0"})).model_copy(update={"title": "edited"})
    reg.save(edited)  # approval no longer matches the content

    pinecrest = {c.ref for c in eligible("pinecrest", reg)}  # AcmeCore 7.2.4
    lakeside = {c.ref for c in eligible("lakeside", reg)}  # AcmeCore 7.3.1
    assert pinecrest == {f"{READ}@1.1.0"}
    assert lakeside == {f"{READ}@1.0.0"}


def test_caller_view_is_the_result_contract_without_evidence_plumbing() -> None:
    r = RunResult(
        run_id="r1",
        mode="replay",
        tenant="pinecrest",
        status="failed",
        started_at="2026-09-25T00:00:00Z",
        error=FailureInfo(code="APP_ERROR", message="server error", retryable=True, evidence=["shot.png"]),
        side_effect="none",
        evidence_dir="/tmp/x",
    )
    view = caller_view(r)
    assert view["error"] == {"code": "APP_ERROR", "message": "server error", "retryable": True}
    assert view["side_effect"] == "none" and view["retry_safe"] is True
    assert "evidence_dir" not in view and "steps" not in view
