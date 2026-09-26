"""A calling agent uses the catalog: it picks a capability by name, execution is deterministic
replay, and the agent gets the typed result contract back (stand-in model, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cua.agent.llm import ScriptedLLM
from cua.catalog import ask
from cua.registry import Registry
from tests.conftest import Bank, approved, load_fixture

pytestmark = pytest.mark.integration


def calling_agent() -> Any:
    def policy(text: str, turn: int) -> tuple[str, dict[str, Any]] | str:
        if turn == 1:
            return "acmecore__member__get_share_balance", {"member_number": "10042"}
        result = json.loads(text)
        if result["status"] == "succeeded":
            return f"The PRIMARY SAVINGS balance is ${result['outputs']['savings_balance']['amount']}."
        return f"I could not get the balance: {result['outcome'] or result['error']}"

    return policy


async def test_agent_invokes_a_capability_by_name_and_gets_typed_results(bank: Bank, tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry")
    reg.save(approved(load_fixture("acmecore.member.get_share_balance")))
    reg.save(load_fixture("acmecore.member.open_share"))  # draft: not offered
    out = await ask(
        "What is the savings balance for member 10042?",
        "pinecrest",
        registry=reg,
        runs_root=tmp_path / "runs",
        llm=ScriptedLLM(calling_agent()),
    )
    assert out["answer"] == "The PRIMARY SAVINGS balance is $12450.31."
    assert [c["status"] for c in out["calls"]] == ["succeeded"]
    evidence = Path(out["evidence_dir"])
    offered = json.loads((evidence / "catalog.json").read_text())
    assert [t["name"] for t in offered] == ["acmecore__member__get_share_balance"]
    blob = "".join(p.read_text() for p in evidence.rglob("*") if p.suffix in (".json", ".jsonl"))
    for raw in ("10042", "12450.31", "12,450.31"):
        assert raw not in blob, f"{raw} leaked into the catalog evidence"
    replay_dir = tmp_path / "runs" / out["calls"][0]["replay_run"]
    assert json.loads((replay_dir / "result.json").read_text())["status"] == "succeeded"
