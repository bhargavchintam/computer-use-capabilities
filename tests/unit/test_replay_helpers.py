from __future__ import annotations

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
