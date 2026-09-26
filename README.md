# computer-use-capabilities

Give an AI agent hands in legacy bank software that has no API.

1. **Discover:** an LLM (Claude) works out how to do a task in a real UI, one grounded action at a time.
2. **Compile:** the successful run becomes a typed, versioned, reviewable **capability**. It has a contract (typed inputs, typed outputs, declared business outcomes) plus an implementation (steps with validated, data-free targets and checkpoints).
3. **Replay:** the capability runs **deterministically with no model in the loop**. It handles runtime errors explicitly, escalates to a human who takes over **the same live session**, and stays inside an allowlist.

The target is a deliberately hostile, fictional core-banking app ("AcmeCore", synthetic data) served locally:

- framesets and layout tables;
- unlabeled inputs and random ids;
- red-text errors;
- a supervisor-PIN step;
- two tenants on different vendor versions;
- switchable faults.

Design and trade-offs: **[REPORT.md](REPORT.md)**. What each evidence run proves: **[evidence/README.md](evidence/README.md)**.

## Quick start (no API key needed)

```bash
make setup      # uv sync + Playwright Chromium; creates .env from .env.example
make mock       # AcmeCore mock: pinecrest (7.2) on :8401, lakeside (7.3) on :8402
make demo       # replay the committed capability: success, not-found, drift, overlay
make test       # 70 tests (unit + real-browser integration); starts its own mock servers
```

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/) (it installs Python 3.12), and about 300 MB for Chromium.

## Configuration

`.env` (git-ignored; copy `.env.example`):

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | discovery only | Replay, tests and evidence generation never call a model. |
| `ANTHROPIC_WORKSPACE_ID` | discovery, if your key is org-level | Sent as the `anthropic-workspace-id` header. |
| `CUA_MODEL` | optional | Defaults to `claude-opus-5`. Server-side refusal fallback is enabled. |
| `MOCK_*`, `PINECREST_*`, `LAKESIDE_*` | mock + runtime | Local demo credentials. The mock and the runtime both read them, so any values work; they exist to exercise secret handling (never shown to the model, scrubbed from all output). |

Tenants, the app profile, the policy allowlist and version overlays live in `config/`. `CUA_TENANT_<ID>_BASE_URL` overrides a tenant's endpoint.

## Demo path: goal → discovery → capability → replay → errors → human handoff

Start the mock (`make mock`), then run the steps in order.

**1. Discover with a real LLM.** Give it a natural-language goal. It compiles the goal to a typed spec, drives the UI, compiles the run, and verifies it by replaying on a second member.

```bash
uv run cua discover --tenant pinecrest \
  --goal "Look up member 10042 and read their current savings balance and their list of shares" \
  --verify-input member_number=10077
```

**2. Review and approve the artifact.** Approval signs its content hash.

```bash
uv run cua capabilities show acmecore.member.get_share_balance
uv run cua capabilities approve acmecore.member.get_share_balance --reviewer "Your Name"
```

**3. Replay with no model involved.**

```bash
uv run cua replay acmecore.member.get_share_balance --tenant pinecrest -i member_number=10042
```

**4. Business outcomes and rejections.**

```bash
uv run cua replay acmecore.member.get_share_balance --tenant pinecrest -i member_number=99999   # MEMBER_NOT_FOUND
uv run cua replay acmecore.member.get_share_balance --tenant pinecrest -i member_number=10013   # ACCOUNT_RESTRICTED
uv run cua replay acmecore.member.get_share_balance --tenant pinecrest -i member_number=12AB5   # rejected: INPUT_INVALID
```

**5. Injected runtime faults.** Each fault is armed for exactly one request.

```bash
uv run cua mock fault maintenance --tenant pinecrest --path /core/inquiry          # recovered: dismissed pop-up
uv run cua mock fault expire      --tenant pinecrest --path /core/inquiry          # recovered: sign on again + re-drive
uv run cua mock fault error500    --tenant pinecrest --path /core/inquiry --count 10  # failed: APP_ERROR (retryable)
```

Run the replay command from step 3 after arming each fault.

**6. Another tenant on a newer vendor version.** It fails with a drift report, then succeeds with the shared 7.3 overlay.

```bash
uv run cua replay acmecore.member.get_share_balance --tenant lakeside -i member_number=20031 --no-overlays
uv run cua replay acmecore.member.get_share_balance --tenant lakeside -i member_number=20031
```

**7. A write flow with a human in the loop.**

```bash
# a commit with no approval parks as needs_human; nothing is committed
uv run cua replay acmecore.member.open_share --tenant pinecrest -i member_number=10042 \
  -i "share_type=Holiday Club" -i initial_deposit=250.00 -i "nickname=Gift fund"

# large deposit: the app demands a supervisor PIN, so the run escalates.
# Open http://127.0.0.1:8765, press "Take control", type the PIN (MOCK_SUPERVISOR_PIN)
# in the SAME browser window, press Approve there, then "Hand control back".
uv run cua replay acmecore.member.open_share --tenant pinecrest -i member_number=10042 \
  -i "share_type=Holiday Club" -i initial_deposit=7500.00 -i "nickname=Trip fund" \
  --approve --operator console
```

**8. Agent-facing catalog (stretch).** Approved capabilities become typed tools; a calling agent invokes them by name, and execution is replay.

```bash
uv run cua catalog export --tenant pinecrest
uv run cua catalog ask "What is the savings balance for member 10042?" --tenant pinecrest
```

Every run writes `runs/<run-id>/`:

- `events.jsonl`: every decision, action, detector, recovery, control transfer and human action, all redacted;
- masked screenshots;
- redacted snapshots;
- `result.json`.

## Running without live services

Everything except discovery is offline: the mock bank runs locally, and replay never imports the model SDK (a test proves it). `make test` starts its own mock servers on random ports and needs no `.env`. It also exercises the discovery loop and compiler through a deterministic scripted stand-in for the model, so it needs no network. `make evidence` regenerates the replay evidence and audits it.

## Repository map

```
src/cua/
  models/           typed contracts: capability artifact, conditions, configs, goal spec, run results
  surface/          Surface seam; Playwright implementation; cu_bundle.js (snapshot + resolver + capture)
  replay.py         deterministic replay engine (race-waits, recoveries, commit gating, result contract)
  runtime.py        shared session: sign-on, dialogs, runtime guard (detectors + bounded recovery)
  agent/            discovery loop, model client, tools, prompts
  compiler.py       grounded trace -> capability (+ fail-closed linter)
  discovery.py      goal -> spec -> run -> compile -> verify-by-replay -> registry
  control.py        control lease, interventions, human-action capture
  operator_console.py, policy.py, redaction.py, tenancy.py, registry.py, catalog.py, evidence.py, cli.py
mock_bank/          the hostile legacy target app (FastAPI, templates, faults, two tenants)
config/             app profile, tenants, policy allowlist, vendor-version overlay
capabilities/       the registry (reviewed YAML artifacts)
schemas/            capability JSON Schema
evidence/           discovery runs, replay runs (incl. errors), handoff, catalog
tests/              unit + integration (real browser against the mock)
```
