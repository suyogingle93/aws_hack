.PHONY: setup test serve demo-agent demo checkpoint dashboard

# Separate npm cache: works even when ~/.npm contains root-owned files.
NPM_CACHE ?= $(HOME)/.cache/npm-harness

setup:
	python3 -m venv venv
	./venv/bin/pip install -r backend/requirements.txt
	./venv/bin/pip install -e backend
	cd dashboard && npm install --cache "$(NPM_CACHE)" --no-fund --no-audit

test:
	cd backend && ../venv/bin/python -m pytest
	cd dashboard && npm test

# Dashboard dev server on http://localhost:5173 (needs `make serve` running).
dashboard:
	cd dashboard && npm run dev

# Start the gateway + control API (leave running in its own terminal).
serve:
	./venv/bin/harness serve

# Run the demo agent directly (no harness) to confirm Bedrock access.
demo-agent:
	./venv/bin/python backend/demo/demo_agent.py

# Run the demo agent under the harness (needs `make serve` in another terminal).
demo:
	./venv/bin/harness run -- ./venv/bin/python backend/demo/demo_agent.py

# Scripted, self-contained demo check: launch -> traffic -> block -> stop.
# Uses its own ports and state, so it can run alongside `make serve`.
checkpoint:
	./venv/bin/python backend/demo/checkpoint.py
