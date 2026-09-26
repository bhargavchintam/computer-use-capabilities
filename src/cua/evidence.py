"""Run evidence: an append-only JSONL event log plus masked screenshots and snapshots.

Everything passes through the redactor before it is written. Playwright traces
are deliberately never recorded: they store typed values (including the
password) and cookies, which no redaction pass could clean reliably.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import Redactor


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunRecorder:
    def __init__(self, runs_root: Path, kind: str, redactor: Redactor) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{stamp}-{kind}-{secrets.token_hex(3)}"
        self.dir = runs_root / self.run_id
        (self.dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._shots = 0
        self._log = open(self.dir / "events.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        self.context: Callable[[], dict[str, Any]] = dict

    def event(self, type_: str, **data: Any) -> dict[str, Any]:
        self._seq += 1
        reserved = {"seq", "ts", "run_id", "type"}
        clash = reserved & data.keys()
        if clash:
            raise ValueError(f"event fields {sorted(clash)} are reserved")
        record = {
            "seq": self._seq,
            "ts": utcnow(),
            "run_id": self.run_id,
            "type": type_,
            **self.context(),
            **data,
        }
        record = self.redactor.scrub(record)
        self._log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._log.flush()
        return record

    def save_screenshot(self, png: bytes | None, label: str) -> str | None:
        if not png:
            return None
        self._shots += 1
        name = f"screenshots/{self._shots:03d}-{label}.png"
        (self.dir / name).write_bytes(png)
        return name

    def save_text(self, name: str, text: str) -> str:
        (self.dir / name).parent.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(self.redactor.scrub_text(text), encoding="utf-8")
        return name

    def save_json(self, name: str, obj: Any, *, scrub: bool = True) -> str:
        (self.dir / name).parent.mkdir(parents=True, exist_ok=True)
        data = self.redactor.scrub(obj) if scrub else obj
        (self.dir / name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return name

    def close(self) -> None:
        if not self._log.closed:
            self._log.close()
