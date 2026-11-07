.PHONY: setup test serve demo-agent demo

setup:
	python3 -m venv venv
	./venv/bin/pip install -r backend/requirements.txt
	./venv/bin/pip install -e backend

test:
	cd backend && ../venv/bin/python -m pytest

# Start the gateway + control API (leave running in its own terminal).
serve:
	./venv/bin/harness serve

# Run the demo agent directly (no harness) to confirm Bedrock access.
demo-agent:
	./venv/bin/python backend/demo/demo_agent.py

# Run the demo agent under the harness (needs `make serve` in another terminal).
demo:
	./venv/bin/harness run -- ./venv/bin/python backend/demo/demo_agent.py
