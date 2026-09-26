"""The evidence audit fails on a planted leak and passes on clean output."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.conftest import ROOT


def audit_main():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("audit_evidence", ROOT / "scripts" / "audit_evidence.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_a_planted_leak_fails_the_audit(tmp_path: Path) -> None:
    main = audit_main()
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "events.jsonl").write_text(
        '{"type": "output_extracted", "value": {"redacted": "confidential"}}\n'
    )
    assert main([clean]) == 0
    for name, text in {
        "name.jsonl": '{"message": "Member HARTWELL, JUNE M found"}\n',  # a synthetic member's name
        "money.json": '{"balance": "$12,450.31"}',
        "ssn.txt": "ssn 900-12-4417",
    }.items():
        leaky = tmp_path / name.split(".")[0]
        leaky.mkdir()
        (leaky / name).write_text(text)
        assert main([leaky]) == 1, name
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / "trace.zip").write_bytes(b"PK")  # traces are not an allowed evidence type
    assert main([odd]) == 1
