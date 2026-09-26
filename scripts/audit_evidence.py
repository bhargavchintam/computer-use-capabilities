"""Audit everything we publish for secrets and sensitive data. Exit 1 on any finding.

Scans evidence/ and capabilities/. Only allowlisted file types may exist there;
every text file is parsed and searched for:
  * values of every secret in .env (when present) and API-key shapes,
  * the mock bank's entire synthetic PII universe (names, SSNs, DOBs, phones,
    addresses, balances, member numbers): if our redaction ever regressed, the
    synthetic values would show up here,
  * SSN/phone/email shapes and dollar amounts.
PNG files are checked for embedded text metadata (pixels are masked at capture
time; see src/cua/surface/web.py).

    uv run python scripts/audit_evidence.py [DIR ...]   # default: evidence/ capabilities/
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mock_bank.data import _LAKESIDE, _PINECREST, money  # noqa: E402

SCAN = [ROOT / "evidence", ROOT / "capabilities"]
TEXT = {".json", ".jsonl", ".yaml", ".yml", ".txt", ".md"}
BINARY = {".png", ".webm"}
PATTERNS = {
    "api key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I),
    "dollar amount": re.compile(r"\$\s?\d[\d,]*\.\d{2}"),
}


def forbidden() -> dict[str, str]:
    values: dict[str, str] = {}
    env = dotenv_values(ROOT / ".env")
    for key, value in env.items():
        if value and key not in ("CUA_MODEL", "ANTHROPIC_WORKSPACE_ID") and len(value) >= 4:
            values[value] = f".env {key}"
    for m in [*_PINECREST, *_LAKESIDE]:
        values[m.name] = "member name"
        values[m.name.split(",")[0]] = "member surname"
        values[m.ssn] = "ssn"
        values[m.dob] = "date of birth"
        values[m.address] = "address"
        values[m.number] = "member number"
        for s in m.shares:
            values[money(s.balance)] = "balance"
            values[f"{s.balance:.2f}"] = "balance"
    return values


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def scan_text(path: Path, text: str, values: dict[str, str]) -> list[str]:
    findings = []
    for value, what in values.items():
        if value.isdigit():  # not inside hashes/longer tokens, and not a JSON number (durations etc.)
            hit = re.search(rf'(?<![\w.])(?<!": )(?<!":){re.escape(value)}(?![\w])', text)
        else:
            hit = re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", text, re.I)
        if hit:
            findings.append(f"{_rel(path)}: contains {what}")
    for what, rx in PATTERNS.items():
        if rx.search(text):
            findings.append(f"{_rel(path)}: {what}-shaped text")
    return findings


def main(bases: list[Path]) -> int:
    values = forbidden()
    findings: list[str] = []
    files = 0
    for base in bases:
        for path in sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []:
            files += 1
            if path.suffix in TEXT:
                findings += scan_text(path, path.read_text(encoding="utf-8", errors="replace"), values)
            elif path.suffix == ".png":
                from PIL import Image

                with Image.open(path) as img:
                    meta = " ".join(str(v) for v in img.info.values())
                findings += scan_text(path, meta, values)
            elif path.suffix not in BINARY:
                findings.append(
                    f"{_rel(path)}: file type {path.suffix or '(none)'} is not allowed in evidence"
                )
    for f in findings:
        print("FINDING", f)
    print(f"audited {files} files: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main([Path(a).resolve() for a in sys.argv[1:]] or SCAN))
