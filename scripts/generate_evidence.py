"""Regenerate the deterministic replay evidence against the running mock bank.

    make mock                                  # both tenants on :8401 / :8402
    uv run python scripts/generate_evidence.py # no API key needed

Uses the capabilities in the registry (the discovered, reviewed artifacts). Each
scenario resets the mock, arms at most one fault, and replays TWICE: the classified
result and the step-trace hash must match (determinism), and the result must be the
expected one. The first run's folder is copied into evidence/replay/.

    uv run python scripts/generate_evidence.py --collect runs/<run-id> discovery/01-read-flow
copies a manual run (discovery, live handoff, catalog) into evidence/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from cua.configio import load_tenant  # noqa: E402
from cua.models import Capability, RunResult  # noqa: E402
from cua.registry import Registry  # noqa: E402
from cua.replay import run_replay  # noqa: E402

READ, WRITE = "acmecore.member.get_share_balance", "acmecore.member.open_share"


def admin(tenant: str, path: str, body: dict[str, Any] | None = None) -> None:
    url = load_tenant(tenant).base_url + path
    r = httpx.post(
        url, json=body, headers={"X-Admin-Token": os.environ.get("MOCK_ADMIN_TOKEN", "change-me-admin")}
    )
    r.raise_for_status()


def scenarios(read: Capability, write: Capability | None) -> list[dict[str, Any]]:
    draft = read.model_copy(update={"status": "draft", "approval": None})
    out = [
        dict(
            name="01-success",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10042"},
            expect=("succeeded", None),
            proves="happy path: typed outputs, success checkpoint verified, primary locators only",
        ),
        dict(
            name="02-member-not-found",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "99999"},
            expect=("business_outcome", "MEMBER_NOT_FOUND"),
            proves="'no such member' is a declared result with caller guidance, not a crash",
        ),
        dict(
            name="03-account-restricted",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10013"},
            expect=("business_outcome", "ACCOUNT_RESTRICTED"),
            proves="permission-type business outcome; guidance says do not retry",
        ),
        dict(
            name="04-input-invalid",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "12AB5"},
            expect=("rejected", "INPUT_INVALID"),
            proves="bad input rejected at pre-flight; the UI is never touched",
        ),
        dict(
            name="05-maintenance-recovered",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10077"},
            fault=dict(kind="maintenance", path_prefix="/core/inquiry"),
            expect=("succeeded", None),
            proves="known interstitial dismissed by a bounded handler; logged in recoveries[]",
        ),
        dict(
            name="06-session-expired-recovered",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10042"},
            fault=dict(kind="expire", path_prefix="/core/inquiry"),
            expect=("succeeded", None),
            proves="session timeout: deterministic re-sign-on + re-drive from entry (nothing committed yet)",
        ),
        dict(
            name="07-app-error-failure",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10042"},
            fault=dict(kind="error500", count=10, path_prefix="/core/inquiry"),
            expect=("failed", "APP_ERROR"),
            proves="bounded retries, then a retryable hard failure with masked screenshot + redacted snapshot",
        ),
        dict(
            name="08-unknown-dialog-fails-safe",
            cap=read,
            tenant="pinecrest",
            inputs={"member_number": "10042"},
            fault=dict(
                kind="alert",
                path_prefix="/core/inquiry",
                message="Posting batch 7 is locked by another user.",
            ),
            expect=("failed", "UNRECOGNIZED_STATE"),
            proves="an unrecognized pop-up is never guessed at: the run stops safely with evidence",
        ),
        dict(
            name="09-production-draft-rejected",
            cap=draft,
            tenant="lakeside",
            inputs={"member_number": "20031"},
            expect=("rejected", "NOT_APPROVED"),
            proves="an unreviewed artifact cannot run on a production tenant",
        ),
        dict(
            name="10-tenant-b-drift-no-overlay",
            cap=read,
            tenant="lakeside",
            inputs={"member_number": "20031"},
            overlays=False,
            expect=("failed", "TARGET_NOT_FOUND"),
            proves="vendor 7.3 renamed a menu item: precise drift failure with near-miss hint",
        ),
        dict(
            name="11-tenant-b-with-overlay",
            cap=read,
            tenant="lakeside",
            inputs={"member_number": "20031"},
            expect=("succeeded", None),
            proves="same artifact + shared 7.3 overlay; fallback locator flagged as drift; reordered columns read by header",
        ),
    ]
    if write is not None:
        base = {"member_number": "10042", "share_type": "Holiday Club", "nickname": "Gift fund"}
        out += [
            dict(
                name="12-commit-parked-needs-approval",
                cap=write,
                tenant="pinecrest",
                inputs={**base, "initial_deposit": "250.00"},
                expect=("needs_human", None),
                proves="a commit without approval parks as needs_human; side_effect not_committed",
            ),
            dict(
                name="13-commit-with-approval",
                cap=write,
                tenant="pinecrest",
                inputs={**base, "initial_deposit": "250.00"},
                approve=True,
                expect=("succeeded", None),
                proves="approved commit: review page echoes every input (pre-checks), side_effect committed",
            ),
        ]
    return out


def classified(r: RunResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "code": r.outcome.code if r.outcome else r.error.code if r.error else None,
        "side_effect": r.side_effect,
        "retry_safe": r.retry_safe,
        "recoveries": [f"{x.detector}: {x.action}" for x in r.recoveries],
        "warnings": [w.code for w in r.warnings],
        "overlays": r.overlays_applied,
        "trace_sha256": r.trace_sha256,
    }


async def main(registry: Registry, read_ref: str, write_ref: str | None, out: Path) -> int:
    read = registry.get(read_ref)
    write = registry.get(write_ref) if write_ref else None
    if not read.approval_valid():
        print(f"{read.ref} must be approved first (cua capabilities approve {read.ref} --reviewer <name>)")
        return 2
    tmp = ROOT / "runs" / "_evidence_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    rows, failures = [], 0
    for sc in scenarios(read, write):
        runs: list[RunResult] = []
        for _ in range(2):  # determinism: same state, same path, same result
            for t in ("pinecrest", "lakeside"):
                admin(t, "/__admin/reset")
            if "fault" in sc:
                admin(sc["tenant"], "/__admin/faults", {"count": 1, **sc["fault"]})
            runs.append(
                await run_replay(
                    sc["cap"],
                    tenant_id=sc["tenant"],
                    inputs=sc["inputs"],
                    approve=sc.get("approve", False),
                    runs_root=tmp,
                    use_overlays=sc.get("overlays", True),
                )
            )
        first, second = (classified(r) for r in runs)
        a, b = runs[0].outputs or {}, runs[1].outputs or {}
        # a read returns the same values; a commit returns a fresh confirmation number each time
        same_outputs = a == b if sc["cap"].contract.effects == "read_only" else a.keys() == b.keys()
        deterministic = first == second and same_outputs
        ok = (first["status"], first["code"] if sc["expect"][1] else None) == sc["expect"] and deterministic
        failures += not ok
        dest = out / "replay" / sc["name"]
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(Path(runs[0].evidence_dir or ""), dest)
        rows.append(
            {
                "scenario": sc["name"],
                "capability": sc["cap"].ref,
                "tenant": sc["tenant"],
                **first,
                "runs": [r.run_id for r in runs],
                "second_run_identical": deterministic,
                "proves": sc["proves"],
                "as_expected": ok,
            }
        )
        print(
            f"{'OK ' if ok else 'BAD'} {sc['name']:34} {first['status']:17} {first['code'] or '':20} "
            f"{first['side_effect']:14} x2 identical={deterministic}"
        )
    (out / "replay" / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    lines = [
        "| scenario | tenant | status | code | side effect | recoveries / warnings | 2nd run identical | what it proves |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        extra = "; ".join(row["recoveries"] + row["warnings"]) or "-"
        lines.append(
            f"| [`{row['scenario']}`]({row['scenario']}/) | {row['tenant']} | **{row['status']}** | "
            f"{row['code'] or '-'} | {row['side_effect']} | {extra} | "
            f"{'yes' if row['second_run_identical'] else '**NO**'} (`{row['trace_sha256'][:12]}`) | {row['proves']} |"
        )
    (out / "replay" / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    for t in ("pinecrest", "lakeside"):
        admin(t, "/__admin/reset")
    return 1 if failures else 0


def collect(run_dir: str, name: str, out: Path) -> int:
    src = Path(run_dir)
    dest = out / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("video", "*.zip"))
    print(f"copied {src} -> {dest}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", default=READ)
    ap.add_argument("--write", default=WRITE)
    ap.add_argument(
        "--registry", type=Path, default=None, help="capability registry (default: capabilities/)"
    )
    ap.add_argument("--out", type=Path, default=ROOT / "evidence", help="evidence root (default: evidence/)")
    ap.add_argument("--collect", nargs=2, metavar=("RUN_DIR", "NAME"))
    args = ap.parse_args()
    if args.collect:
        sys.exit(collect(*args.collect, args.out))
    reg = Registry(args.registry)
    try:
        reg.get(args.write)
    except LookupError:
        args.write = None
    sys.exit(asyncio.run(main(reg, args.read, args.write, args.out)))
