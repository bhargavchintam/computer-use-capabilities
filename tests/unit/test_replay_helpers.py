from __future__ import annotations

from pathlib import Path

import pytest

from cua.replay import parse_value, validate_inputs
from tests.conftest import load_fixture


def test_input_validation_against_the_contract() -> None:
    cap = load_fixture("acmecore.member.open_share")
    good = {
        "member_number": "10042",
        "share_type": "Holiday Club",
        "initial_deposit": "250.00",
        "nickname": "Gift fund",
    }
    assert validate_inputs(cap, good) == (good, [])
    _, errors = validate_inputs(
        cap,
        {
            **good,
            "member_number": "1004",
            "share_type": "Gold Club",
            "initial_deposit": "99999.00",
            "extra": "x",
        },
    )
    joined = " | ".join(errors)
    assert "member_number" in joined and "share_type" in joined and "maximum" in joined and "extra" in joined
    assert "1004" not in joined  # errors never echo values
    _, errors = validate_inputs(cap, {k: v for k, v in good.items() if k != "nickname"})
    assert errors == ["nickname: required"]


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("$12,450.31", "money", {"amount": "12450.31", "currency": "USD"}),
        ("-$5.00", "money", {"amount": "-5.00", "currency": "USD"}),
        # ledger screens write negatives in several ways; the sign must never be lost
        ("($1,234.56)", "money", {"amount": "-1234.56", "currency": "USD"}),
        ("1,234.56-", "money", {"amount": "-1234.56", "currency": "USD"}),
        ("1,234.56 DR", "money", {"amount": "-1234.56", "currency": "USD"}),
        ("1,234.56 CR", "money", {"amount": "1234.56", "currency": "USD"}),
        ("1,204", "integer", 1204),
        ("  CN7K2Q9D4X ", "identifier", "CN7K2Q9D4X"),
    ],
)
def test_parse_values(text: str, kind: str, expected: object) -> None:
    assert parse_value(text, kind) == expected


def test_parse_rejects_non_values() -> None:
    with pytest.raises(ValueError):
        parse_value("n/a", "money")
    with pytest.raises(ValueError):
        parse_value("   ", "identifier")


def test_a_person_using_a_commit_control_makes_the_side_effect_unknown(tmp_path: Path) -> None:
    """Whatever a person does while holding the session goes through the same effect
    classification: a commit-class click is never reported as "nothing happened"."""
    from types import SimpleNamespace

    from cua.configio import load_app_profile, load_overlays, load_policy, load_tenant
    from cua.control import InterventionRequest
    from cua.control import Resolution as Handoff
    from cua.evidence import RunRecorder
    from cua.policy import PolicyEngine
    from cua.redaction import Redactor
    from cua.replay import ReplayEngine
    from cua.tenancy import resolve_plan

    app, tenant, policy = load_app_profile("acmecore"), load_tenant("pinecrest"), load_policy("default")
    cap = load_fixture("acmecore.member.get_share_balance")
    plan = resolve_plan(cap, tenant, app, policy, load_overlays())
    redactor = Redactor(app.sensitive_labels)
    recorder = RunRecorder(tmp_path, "replay", redactor)
    session = SimpleNamespace(surface=None, app=app, secrets={}, recorder=recorder)
    engine = ReplayEngine(
        plan,
        session=session,  # type: ignore[arg-type]
        controller=SimpleNamespace(),  # type: ignore[arg-type]
        recorder=recorder,
        redactor=redactor,
        policy=PolicyEngine(policy, tenant, app),
        inputs={"member_number": "10042"},
        tenant_version=tenant.product_version,
        invocation_approved=False,
    )
    assert engine.side_effect() == "none"
    req = InterventionRequest(
        id="iv-1",
        run_id="r",
        kind="implicit_takeover",
        reason="x",
        allowed_decisions=["hand_back"],
        requested_at="t",
        deadline="t",
    )
    looked = {"payload": {"type": "click", "describe": {"control": {"role": "link", "name": "Reports"}}}}
    engine._note_human_effects(Handoff(req, "hand_back", [], [looked]))
    assert engine.side_effect() == "none"
    confirmed = {
        "payload": {
            "type": "click",
            "describe": {
                "control": {
                    "role": "button",
                    "name": "Confirm",
                    "submits": True,
                    "form_action": "/core/member/10042/newshare/confirm",
                }
            },
        }
    }
    engine._note_human_effects(Handoff(req, "hand_back", [], [confirmed]))
    assert engine.side_effect() == "unknown"
    recorder.close()
