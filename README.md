# computer-use-capabilities

Give an AI agent hands in legacy bank software that has no API.

1. **Discover.** An LLM (Claude Opus 5) works out how to do a task in a live UI, one grounded action at a time.
2. **Compile.** The successful run becomes a typed, versioned, reviewable **capability**:
   - a contract: typed inputs, typed outputs, declared business outcomes;
   - an implementation: steps with validated, data-free targets and checkpoints.
3. **Replay.** The capability runs **deterministically, with no model in the loop**. It:
   - classifies every runtime condition;
   - never commits twice;
   - hands the **same live session** to a person when it must;
   - stays inside an allowlist.

The target is **AcmeCore**, a fictional core-banking app with synthetic data, served locally and hostile on purpose:

- framesets and layout tables;
- unlabeled inputs and random ids;
- red-text errors;
- a supervisor-PIN step;
- two tenants on different vendor versions;
- switchable faults.

Design and trade-offs: **[REPORT.md](REPORT.md)**. What each run proves: **[evidence/README.md](evidence/README.md)**.

## Quick start (no API key needed)

```bash
make setup      # uv sync + Playwright Chromium; creates .env from .env.example
make mock       # AcmeCore mock: pinecrest (7.2, sandbox) on :8401, lakeside (7.3, production) on :8402
make demo       # replay the discovered capabilities: success, not found, drift, overlay
make test       # unit + real-browser integration tests; starts its own mock servers
```

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/) (it installs Python 3.12), and about 300 MB for Chromium.

## Configuration

`.env` is git-ignored; copy it from `.env.example`.

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | discovery and `catalog ask` | Replay, tests and evidence generation never call a model. |
| `ANTHROPIC_WORKSPACE_ID` | discovery, if your key is org-level | Sent as the `anthropic-workspace-id` header. |
| `CUA_MODEL` | optional | Defaults to `claude-opus-5`. |
| `MOCK_*`, `PINECREST_*`, `LAKESIDE_*` | mock and runtime | Local demo credentials that both the mock and the runtime read. They exercise secret handling: never shown to the model, scrubbed from all output. |

`config/` holds:

- the app profile (vendor-product knowledge);
- tenants (base URL, product version, sandbox or production);
- the policy allowlist;
- vendor-version overlays.

`CUA_TENANT_<ID>_BASE_URL` overrides a tenant's endpoint. A goal's *target* is a tenant: its app, version, base URL and entry point.

## Demo path: goal → discovery → capability → replay → errors → human handoff

Start the mock (`make mock`), then run the steps in order. The capabilities discovered this way are already committed in [`capabilities/`](capabilities/), so steps 3 onward work without an API key.

**1. Discover with a real LLM.** Give it a goal in plain English. It:

- compiles the goal to a typed spec;
- drives the UI;
- compiles the run into a capability;
- verifies it by replaying on a second member.

Discovery runs only on sandbox tenants.

```bash
uv run cua discover --tenant pinecrest \
  --goal "Look up member 10042 and read their current savings balance and their list of shares" \
  --verify-input member_number=10077
```

A write goal also needs a person to approve the commit. It opens a browser window and the operator console at http://127.0.0.1:8765. Approve once for the discovery run, then again for the verification commit.

```bash
uv run cua discover --tenant pinecrest --operator console \
  --goal "Open a new Holiday Club share for member 10042 with an initial deposit of 250.00 and the nickname Gift fund, and return the confirmation number and the new share ID" \
  --verify-input member_number=10077 --verify-input "nickname=Verify run"
```

**2. Review and approve.** The approval records the artifact's content hash; any later edit turns it back into a draft. Overlays are approved the same way.

```bash
uv run cua capabilities show acmecore.member.get_savings_balance_and_shares
uv run cua capabilities approve acmecore.member.get_savings_balance_and_shares --reviewer "Your Name"
uv run cua overlays list
```

**3. Replay with no model.**

```bash
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=10042
```

**4. Business outcomes and rejections.**

```bash
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=99999   # MEMBER_NOT_FOUND
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=10091   # OUTPUT_NOT_PRESENT (no savings)
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=12AB5   # rejected: INPUT_INVALID
```

**5. Injected runtime faults.** Each fault is armed for exactly one request; run the replay from step 3 after each.

```bash
uv run cua mock fault maintenance --tenant pinecrest --path /core/inquiry              # recovered: dismissed pop-up
uv run cua mock fault expire --tenant pinecrest --path /core/inquiry                   # recovered: sign on again, re-drive
uv run cua mock fault error500 --tenant pinecrest --path /core/inquiry --count 10      # failed: APP_ERROR, transient
```

**6. Another tenant on a newer vendor version.** It fails with a drift report, then succeeds with the shared, approved 7.3 overlay.

```bash
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant lakeside -i member_number=20031 --no-overlays
uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant lakeside -i member_number=20031
```

**7. A write flow and a person on the live session.**

```bash
# a commit with no approval parks as needs_human; nothing is committed
uv run cua replay acmecore.share.open_share_account --tenant pinecrest -i member_number=10042 \
  -i "share_product=Holiday Club" -i initial_deposit=250.00 -i "nickname=Gift fund"

# a $7,500 deposit needs a supervisor PIN, so the run escalates to the console
uv run cua replay acmecore.share.open_share_account --tenant pinecrest -i member_number=10042 \
  -i "share_product=Holiday Club" -i initial_deposit=7500.00 -i "nickname=Trip fund" \
  --approve --operator console
```

For the second command:

1. Open http://127.0.0.1:8765 and click **Take control of the live session**.
2. In the browser window, type the PIN (`MOCK_SUPERVISOR_PIN`, `2468` in `.env.example`) and click **Approve**.
3. When the console says **STEP COMPLETE**, click **Hand control back to automation**.

Replay then re-checks the receipt, reads the confirmation number and reports `side_effect: committed`, with your actions recorded and the PIN not captured.

**8. Agent-facing catalog (stretch).** Approved capabilities become strict tools generated from their contracts. A calling agent invokes them by name, and execution is replay.

```bash
uv run cua catalog export --tenant pinecrest
uv run cua catalog ask "What is the savings balance for member 10042?" --tenant pinecrest
```

Every run writes `runs/<run-id>/`, all redacted:

- `events.jsonl`: every decision, action, detector, recovery, control transfer and human action;
- masked screenshots;
- redacted snapshots;
- `result.json`, the result contract (JSON Schema in [`schemas/run_result.schema.json`](schemas/run_result.schema.json)).

## Running without live services

Everything except discovery and `catalog ask` is offline. The mock bank runs locally, and replay never imports the model SDK (a test proves it).

`make test` needs no `.env`: it starts its own mock servers on random ports, and runs the discovery loop and compiler against a deterministic stand-in for the model. `make evidence` regenerates the replay evidence (each scenario twice) and audits it.

## Repository map

```
src/cua/
  models/           typed contracts: capability artifact, conditions, configs, goal spec, run result
  surface/          the Surface protocol (the seam); Playwright implementation; cu_bundle.js (snapshot, resolver, capture)
  replay.py         deterministic replay (race-waits, recoveries, commit safety, escalation, result contract)
  runtime.py        shared session: sign-on, dialogs, runtime guard (detectors + bounded recovery)
  agent/            discovery loop, model client, tools, prompts
  compiler.py       grounded trace -> capability, and the fail-closed linter
  discovery.py      goal -> spec -> run -> compile -> verify-by-replay -> registry
  control.py        control lease, interventions, human-action capture
  operator_console.py, policy.py, redaction.py, tenancy.py, registry.py, catalog.py, evidence.py, cli.py
mock_bank/          the hostile legacy target app (FastAPI, templates, faults, two tenants)
config/             app profile, tenants, policy allowlist, approved vendor-version overlay
capabilities/       the registry: the discovered, reviewed artifacts
schemas/            JSON Schemas: capability artifact and run result
evidence/           real discovery runs, replay scenarios, live handoffs, catalog
tests/              unit + integration (real browser against the mock)
scripts/            evidence generator, evidence audit
```
