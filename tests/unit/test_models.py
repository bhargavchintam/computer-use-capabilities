from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from cua.configio import load_app_profile, load_overlays, load_policy, load_tenant, model_to_yaml
from cua.models import Capability
from tests.conftest import load_fixture


def test_fixtures_and_configs_validate() -> None:
    assert load_app_profile("acmecore").product == "acmecore"
    assert {load_tenant("pinecrest").environment, load_tenant("lakeside").environment} == {
        "sandbox",
        "production",
    }
    assert load_policy("default").commit.require_approval
    assert [o.id for o in load_overlays()] == ["acmecore-7.3"]
    for name in ("acmecore.member.get_share_balance", "acmecore.member.open_share"):
        assert load_fixture(name).schema_version == "1.0"


def test_yaml_round_trip_keeps_the_content_hash() -> None:
    cap = load_fixture("acmecore.member.open_share")
    again = Capability.model_validate(yaml.safe_load(model_to_yaml(cap)))
    assert again.content_sha256() == cap.content_sha256()


def test_status_and_approval_are_outside_the_hash() -> None:
    cap = load_fixture("acmecore.member.get_share_balance")
    assert cap.model_copy(update={"status": "approved"}).content_sha256() == cap.content_sha256()
    assert cap.model_copy(update={"title": "x"}).content_sha256() != cap.content_sha256()


def _data() -> dict:  # type: ignore[type-arg]
    return load_fixture("acmecore.member.get_share_balance").model_dump(mode="json", exclude_none=True)


def test_strict_types_reject_an_unquoted_member_number() -> None:
    data = _data()
    data["contract"]["example"] = {"input": {"member_number": 10042}, "output": {}}
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_unknown_param_reference_is_rejected() -> None:
    data = _data()
    data["implementation"]["steps"][1]["value"] = {"param": "account_number"}
    with pytest.raises(ValidationError, match="unknown input"):
        Capability.model_validate(data)


def test_outputs_must_match_extract_steps() -> None:
    data = _data()
    data["implementation"]["steps"] = data["implementation"]["steps"][:-1]
    with pytest.raises(ValidationError, match="must match contract outputs"):
        Capability.model_validate(data)


def test_effects_must_match_commit_steps() -> None:
    data = _data()
    data["implementation"]["steps"][2]["effect"] = "commit"
    with pytest.raises(ValidationError, match="effects"):
        Capability.model_validate(data)


def test_only_clicks_can_commit_and_extracts_are_reads() -> None:
    data = _data()
    data["implementation"]["steps"][1]["effect"] = "commit"
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_unknown_keys_are_rejected() -> None:
    data = _data()
    data["implementation"]["steps"][0]["selector"] = "#ctl00_x1"
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_json_schema_exports() -> None:
    schema = Capability.model_json_schema()
    assert "contract" in schema["properties"] and "implementation" in schema["properties"]
    json.dumps(schema)
