"""Determinism beyond the happy path: the same capability on the same application state takes the
same path and returns the same classified result, for every class of runtime condition.

Each scenario runs twice from a freshly reset mock (faults re-armed) and must agree on status,
code, side effect, recoveries, warnings, outputs and the step-trace hash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua.models import Capability, RunResult
from cua.replay import run_replay
from tests.conftest import Bank, approved

pytestmark = pytest.mark.integration

SCENARIOS: list[tuple[str, str, str, dict[str, Any] | None, bool, tuple[str, str | None]]] = [
    ("not-found", "pinecrest", "99999", None, True, ("business_outcome", "MEMBER_NOT_FOUND")),
    ("restricted", "pinecrest", "10013", None, True, ("business_outcome", "ACCOUNT_RESTRICTED")),
    (
        "maintenance",
        "pinecrest",
        "10077",
        {"kind": "maintenance", "path": "/core/inquiry"},
        True,
        ("succeeded", None),
    ),
    (
        "session-expired",
        "pinecrest",
        "10042",
        {"kind": "expire", "path": "/core/inquiry"},
        True,
        ("succeeded", None),
    ),
    (
        "persistent-500",
        "pinecrest",
        "10042",
        {"kind": "error500", "path": "/core/inquiry", "count": 10},
        True,
        ("failed", "APP_ERROR"),
    ),
    ("drift-no-overlay", "lakeside", "20031", None, False, ("failed", "TARGET_NOT_FOUND")),
    ("overlay", "lakeside", "20031", None, True, ("succeeded", None)),
]


def classified(r: RunResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "code": r.outcome.code if r.outcome else r.error.code if r.error else None,
        "side_effect": r.side_effect,
        "retry_safe": r.retry_safe,
        "recoveries": [(x.detector, x.step_id, x.action) for x in r.recoveries],
        "warnings": [(w.code, w.step_id) for w in r.warnings],
        "outputs": r.outputs,
        "path": r.path_sha256,
    }


@pytest.mark.parametrize(
    ("name", "tenant", "member", "fault", "overlays", "expected"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
async def test_same_state_same_path_same_result(
    bank: Bank,
    balance_cap: Capability,
    tmp_path: Path,
    name: str,
    tenant: str,
    member: str,
    fault: dict[str, Any] | None,
    overlays: bool,
    expected: tuple[str, str | None],
) -> None:
    cap = approved(balance_cap)
    runs = []
    for _ in range(2):
        bank.reset()
        if fault:
            bank.fault(tenant, **fault)
        runs.append(
            classified(
                await run_replay(
                    cap,
                    tenant_id=tenant,
                    inputs={"member_number": member},
                    runs_root=tmp_path,
                    use_overlays=overlays,
                )
            )
        )
    first, second = runs
    assert (first["status"], first["code"]) == expected, first
    assert first == second
