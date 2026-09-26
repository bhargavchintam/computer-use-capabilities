"""Shared fixtures: in-process mock bank servers on free ports, fixture capabilities, helpers."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
for key, default in {
    "MOCK_ADMIN_TOKEN": "test-admin-token",
    "MOCK_SUPERVISOR_PIN": "2468",
    "PINECREST_OPERATOR_ID": "teller01",
    "PINECREST_OPERATOR_PASSWORD": "test-pinecrest-pw",
    "LAKESIDE_OPERATOR_ID": "teller07",
    "LAKESIDE_OPERATOR_PASSWORD": "test-lakeside-pw",
}.items():
    os.environ.setdefault(key, default)

import uvicorn  # noqa: E402

from cua.configio import load_model  # noqa: E402
from cua.models import Approval, Capability  # noqa: E402
from mock_bank.app import create_app  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "capabilities" / "acmecore"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Bank:
    def __init__(self, ports: dict[str, int]) -> None:
        self.ports = ports

    def url(self, tenant: str) -> str:
        return f"http://127.0.0.1:{self.ports[tenant]}"

    def admin(self, tenant: str, path: str, body: dict[str, Any] | None = None) -> Any:
        r = httpx.post(
            self.url(tenant) + path, json=body, headers={"X-Admin-Token": os.environ["MOCK_ADMIN_TOKEN"]}
        )
        r.raise_for_status()
        return r.json()

    def fault(
        self,
        tenant: str,
        kind: str,
        *,
        count: int = 1,
        path: str = "/core/",
        delay_ms: int = 0,
        message: str = "",
    ) -> None:
        self.admin(
            tenant,
            "/__admin/faults",
            {"kind": kind, "count": count, "path_prefix": path, "delay_ms": delay_ms, "message": message},
        )

    def state(self, tenant: str) -> Any:
        r = httpx.get(
            self.url(tenant) + "/__admin/state", headers={"X-Admin-Token": os.environ["MOCK_ADMIN_TOKEN"]}
        )
        return r.json()

    def reset(self) -> None:
        for t in self.ports:
            self.admin(t, "/__admin/reset")


@pytest.fixture(scope="session")
def bank() -> Any:
    servers = []
    ports: dict[str, int] = {}
    for tenant in ("pinecrest", "lakeside"):
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(create_app(tenant), host="127.0.0.1", port=port, log_level="warning")
        )
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        os.environ[f"CUA_TENANT_{tenant.upper()}_BASE_URL"] = f"http://127.0.0.1:{port}"
        ports[tenant] = port
        servers.append(server)
    yield Bank(ports)
    for s in servers:
        s.should_exit = True


@pytest.fixture(autouse=True)
def _reset_bank(request: pytest.FixtureRequest) -> None:
    if "bank" in request.fixturenames:
        request.getfixturevalue("bank").reset()


def load_fixture(name: str) -> Capability:
    return load_model(FIXTURES / name / "1.0.0.yaml", Capability)


def approved(cap: Capability, reviewer: str = "test-reviewer") -> Capability:
    return cap.model_copy(
        update={
            "status": "approved",
            "approval": Approval(
                approved_by=reviewer, approved_at="2026-09-25T00:00:00Z", content_sha256=cap.content_sha256()
            ),
        }
    )


@pytest.fixture
def balance_cap() -> Capability:
    return load_fixture("acmecore.member.get_share_balance")


@pytest.fixture
def share_cap() -> Capability:
    return load_fixture("acmecore.member.open_share")
