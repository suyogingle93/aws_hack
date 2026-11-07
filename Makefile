.PHONY: setup test demo-agent

setup:
	python3 -m venv venv
	./venv/bin/pip install -r backend/requirements.txt

test:
	cd backend && ../venv/bin/python -m pytest

# Run the demo agent directly (no harness) to confirm Bedrock access.
demo-agent:
	./venv/bin/python backend/demo/demo_agent.py
