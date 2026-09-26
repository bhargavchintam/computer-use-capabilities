"""Fault injection for the mock bank.

Faults are armed through the token-protected admin endpoint (never part of any
automation allowlist) and each one fires a fixed number of times, so a test or
demo can say "the next member page shows the maintenance notice once".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FaultKind = Literal["maintenance", "alert", "error500", "slow"]


@dataclass
class Fault:
    kind: FaultKind
    remaining: int = 1
    path_prefix: str = "/"
    delay_ms: int = 0
    message: str = ""


class FaultRegistry:
    def __init__(self) -> None:
        self._faults: list[Fault] = []

    def arm(self, fault: Fault) -> None:
        self._faults.append(fault)

    def take(self, kind: FaultKind, path: str) -> Fault | None:
        """Consume one firing of the first armed fault of `kind` matching `path`."""
        for f in self._faults:
            if f.kind == kind and path.startswith(f.path_prefix) and f.remaining > 0:
                f.remaining -= 1
                if f.remaining == 0:
                    self._faults.remove(f)
                return f
        return None

    def clear(self) -> None:
        self._faults.clear()

    def describe(self) -> list[dict[str, object]]:
        return [f.__dict__.copy() for f in self._faults]
