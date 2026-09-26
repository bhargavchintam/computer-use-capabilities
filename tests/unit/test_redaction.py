from __future__ import annotations

from cua.redaction import Redactor, money_shape


def make() -> Redactor:
    r = Redactor(["Name", "SSN", "Date of Birth"])
    r.add_secret("operator_password", "pc-Teller-9x41")
    r.add_param("member_number", "10042", "pii_identifier")
    r.add_param("nickname", "Gift fund", "internal")
    r.add_param("initial_deposit", "7500.00", "confidential")
    return r


def test_secrets_are_scrubbed_everywhere() -> None:
    r = make()
    assert "pc-Teller-9x41" not in r.scrub_text("login with pc-Teller-9x41 failed")
    assert r.scrub_text("key sk-ant-api03-abcdefghijklmnop") == "key <api-key>"


def test_inputs_become_placeholders_for_the_model_and_masks_in_logs() -> None:
    r = make()
    assert r.scrub_text("Member # 10042 found", for_model=True) == "Member # {{member_number}} found"
    assert r.scrub_text("Member # 10042 found") == "Member # <member_number:…42> found"
    assert r.scrub_text("nickname Gift fund") == "nickname Gift fund"  # internal values stay readable in logs
    assert "7500.00" not in r.scrub_text("deposit 7500.00")


def test_input_matching_respects_word_boundaries() -> None:
    r = make()
    assert r.scrub_text("share 100420 and 10042", for_model=True) == "share 100420 and {{member_number}}"


def test_pii_patterns() -> None:
    r = Redactor()
    text = "ssn 900-12-4417 masked ***-**-4417 phone (206) 555-0142 mail a.b@example.com card 4111 1111 1111 1111"
    out = r.scrub_text(text)
    for leaked in ("900-12-4417", "4417", "555-0142", "a.b@example.com", "4111"):
        assert leaked not in out


def test_capability_references_are_not_mistaken_for_emails() -> None:
    r = Redactor()
    ref = "acmecore.member.get_share_balance@1.0.0"
    assert r.scrub_text(f"replaying {ref}") == f"replaying {ref}"
    assert r.scrub_text("contact ops-team@cu.example.org") == "contact <email>"


def test_money_keeps_shape_for_the_model_only() -> None:
    r = Redactor()
    assert r.scrub_text("Balance $12,450.31", for_model=True) == "Balance $##,###.##"
    assert r.scrub_text("Balance $12,450.31") == "Balance <money>"
    assert money_shape("$600.00") == "$###.##"


def test_values_next_to_sensitive_labels_are_masked() -> None:
    r = make()
    assert r.scrub_value_for_label("Name:", "HARTWELL, JUNE M", for_model=True) == "<pii:name>"
    assert r.scrub_value_for_label("Member #:", "10042", for_model=True) == "{{member_number}}"


def test_outputs_in_logs_are_digests() -> None:
    r = Redactor()
    logged = r.output_for_log({"amount": "12450.31", "currency": "USD"}, "confidential")
    assert logged["redacted"] == "confidential" and "12450" not in str(logged)
    assert r.output_for_log("CN7K2Q9D4X", "internal") == "CN7K2Q9D4X"
