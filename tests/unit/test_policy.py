from __future__ import annotations

from cua.configio import load_app_profile, load_policy, load_tenant
from cua.policy import PolicyEngine
from tests.conftest import approved, load_fixture


def engine(tenant: str = "pinecrest") -> PolicyEngine:
    t = load_tenant(tenant).model_copy(update={"base_url": "http://127.0.0.1:8401"})
    return PolicyEngine(load_policy("default"), t, load_app_profile("acmecore"))


def test_allowlist_by_origin_and_path() -> None:
    p = engine()
    assert p.url_allowed("http://127.0.0.1:8401/core/inquiry").allowed
    assert p.url_allowed("http://127.0.0.1:8401/static/btn/x.svg").allowed
    assert not p.url_allowed("http://127.0.0.1:8401/__admin/faults").allowed  # test hooks are never reachable
    assert not p.url_allowed("https://evil.example/exfil?d=1").allowed
    assert not p.url_allowed("http://127.0.0.1:9999/core/inquiry").allowed  # another tenant's origin
    assert not p.url_allowed("http://127.0.0.1:8401/admin/users").allowed  # not on the allowlist
    assert p.url_allowed("about:blank").allowed


def test_action_types() -> None:
    p = engine()
    assert p.action_allowed("click").allowed and p.action_allowed("extract_table").allowed
    assert not p.action_allowed("upload").allowed


def test_effect_classification() -> None:
    p = engine()
    assert p.classify("click", {"name": "Search", "submits": True, "form_action": "/core/inquiry"}) == "read"
    assert (
        p.classify(
            "click", {"name": "Confirm", "submits": True, "form_action": "/core/member/1/newshare/confirm"}
        )
        == "commit"
    )
    # a harmless-looking name that submits to a commit route is still a commit
    assert (
        p.classify(
            "click", {"name": "OK", "submits": True, "form_action": "/core/member/1/newshare/override"}
        )
        == "commit"
    )
    assert p.classify("fill", {"name": ""}) == "input"
    assert p.classify("extract", None) == "read"
    # recorded effects can raise but never lower the class
    assert p.classify("click", {"name": "Search"}, recorded="commit") == "commit"
    assert p.classify("click", {"name": "Confirm"}, recorded="read") == "commit"


def test_commit_gate_needs_explicit_approval() -> None:
    p = engine()
    assert p.commit_gate(approved=False) == "needs_approval"
    assert p.commit_gate(approved=True) == "allow"


def test_preflight_production_needs_a_valid_approval() -> None:
    cap = load_fixture("acmecore.member.get_share_balance")
    assert engine("pinecrest").preflight(cap) == []  # sandbox: drafts allowed
    codes = [c for c, _ in engine("lakeside").preflight(cap)]
    assert codes == ["NOT_APPROVED"]
    assert engine("lakeside").preflight(approved(cap)) == []
    edited = approved(cap).model_copy(update={"description": "changed after review"})
    problems = engine("lakeside").preflight(edited)
    assert problems and "edited after approval" in problems[0][1]


def test_anything_that_can_send_a_commit_form_is_a_commit() -> None:
    p = engine()
    confirm_form = "/core/member/10042/newshare/confirm"
    # a button that submits by script, not a native submit
    assert p.classify("click", {"role": "button", "name": "OK", "form_action": confirm_form}) == "commit"
    # Enter in any field of a commit form
    assert p.classify("press_key", {"role": "textbox", "form_action": confirm_form}) == "commit"
    # a link that goes straight to a commit route
    assert p.classify("click", {"role": "link", "name": "Finish", "href": confirm_form}) == "commit"
    # ordinary navigation stays a read
    assert p.classify("click", {"role": "link", "name": "Member Inquiry", "href": "/core/inquiry"}) == "read"


def test_production_needs_approved_overlays_too() -> None:
    from cua.configio import load_overlays

    cap = approved(load_fixture("acmecore.member.get_share_balance"))
    overlay = load_overlays()[0]
    assert overlay.approval_valid()
    assert engine("lakeside").preflight(cap, [overlay]) == []
    draft = overlay.model_copy(update={"status": "draft", "approval": None})
    problems = engine("lakeside").preflight(cap, [draft])
    assert [c for c, _ in problems] == ["NOT_APPROVED"] and "overlay acmecore-7.3" in problems[0][1]
    edited = overlay.model_copy(update={"description": "changed after review"})
    assert "edited after approval" in engine("lakeside").preflight(cap, [edited])[0][1]
    assert engine("pinecrest").preflight(cap, [draft]) == []  # sandbox: drafts allowed
