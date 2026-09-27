.PHONY: setup mock mock-stop test test-unit lint schema evidence audit demo clean

setup:          ## install dependencies and the Chromium build Playwright needs
	uv sync
	uv run playwright install chromium
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example (add ANTHROPIC_API_KEY for discovery)")

mock:           ## start both mock tenants in the background (:8401 pinecrest 7.2, :8402 lakeside 7.3)
	@mkdir -p runs
	@uv run python -m mock_bank --tenant pinecrest > runs/mock-pinecrest.log 2>&1 & echo $$! > .mock_pids
	@uv run python -m mock_bank --tenant lakeside > runs/mock-lakeside.log 2>&1 & echo $$! >> .mock_pids
	@sleep 2 && curl -sf -o /dev/null http://127.0.0.1:8401/signon && curl -sf -o /dev/null http://127.0.0.1:8402/signon \
		&& echo "mock bank up: http://127.0.0.1:8401 (pinecrest) http://127.0.0.1:8402 (lakeside)"

mock-stop:       ## stop both mock tenants (the `uv run` wrappers and the servers they started)
	@-test -f .mock_pids && xargs kill < .mock_pids 2>/dev/null
	@-pkill -f "mock_bank --tenant" 2>/dev/null
	@rm -f .mock_pids

test:           ## unit + integration tests (starts its own mock servers; no API key needed)
	uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

lint:
	uv run ruff check src tests mock_bank scripts
	uv run ruff format --check src tests mock_bank scripts
	uv run mypy src/cua

schema:         ## export the capability and run-result JSON Schemas
	uv run cua capabilities schema

evidence:       ## regenerate deterministic replay evidence (needs `make mock`), then audit it
	uv run python scripts/generate_evidence.py
	uv run python scripts/audit_evidence.py

audit:
	uv run python scripts/audit_evidence.py

demo:           ## offline demo: replay the discovered capabilities (needs `make mock`; no API key)
	uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=10042
	uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant pinecrest -i member_number=99999 || true
	uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant lakeside -i member_number=20031 --no-overlays || true
	uv run cua replay acmecore.member.get_savings_balance_and_shares --tenant lakeside -i member_number=20031

clean:
	rm -rf runs .pytest_cache .ruff_cache
