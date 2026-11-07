# Implementation Plan

Ordering goal: get the thin demo path (launch, proxy, block, stop) working first, then add the dashboard, persistence, and polish. Each task builds on the previous ones.

- [ ] 1. Scaffold the project
  - Create `backend/` (Python 3.11+, `pyproject.toml`, deps: mitmproxy, fastapi, uvicorn, typer, pydantic, pyyaml, boto3, httpx, pytest) and `dashboard/` (Vite + React + TypeScript)
  - Add a `Makefile` with `dev`, `test`, `demo` targets and a `.gitignore`
  - _Requirements: 8.3_

- [x] 2. Implement the policy engine
  - [x] 2.1 Define Pydantic models for `Rule`, `Policy`, `Decision` and YAML loading with validation
    - _Requirements: 4.1, 4.5_
  - [x] 2.2 Implement evaluation: wildcard matching on host/path/method/mcp_tool, deny precedence, default action
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 2.3 Add per-run sliding-window rate limiting
    - _Requirements: 4.7_
  - [x] 2.4 Add atomic policy swap and reload that keeps the old policy on invalid input
    - _Requirements: 4.5, 4.6_
  - [x] 2.5 Write unit tests, including deny-beats-allow regardless of rule order
    - _Requirements: 4.2, 4.3_
  - [x] 2.6 Create `policies/demo.yaml` with allow rules for Bedrock/GitHub and a deny rule with `terminate_on_violation`
    - _Requirements: 8.2_

- [x] 3. Implement redaction and MCP parsing
  - [x] 3.1 Build `redact.py` for sensitive headers and token-like patterns, with tests
    - _Requirements: 6.3, 9.3_
  - [x] 3.2 Build `mcp.py` to extract tool name and arguments from JSON-RPC `tools/call`, including batches, with tests
    - _Requirements: 2.5_

- [x] 4. Implement the event bus and storage
  - [x] 4.1 Define the `Event` model and an asyncio `EventBus` with bounded per-subscriber queues
    - _Requirements: 3.1, 3.2_
  - [x] 4.2 Define the `AuditStore` interface and the SQLite implementation (events and runs, query by run and time range)
    - _Requirements: 6.1, 6.2, 6.4_
  - [x] 4.3 Wire the bus to the store through a background writer that retries on failure
    - _Requirements: 6.1_

- [x] 5. Implement the run manager
  - [x] 5.1 Create the `Run` model, token generation (hash only stored), constant-time verification
    - _Requirements: 2.2, 9.1_
  - [x] 5.2 Implement `stop()`: mark stopped, revoke token, SIGTERM then SIGKILL on the process group, publish a stop event
    - _Requirements: 5.1, 5.2, 5.3, 5.5_
  - [x] 5.3 Write tests that stop a real child process tree and assert it exits within 3 seconds
    - _Requirements: 5.1, 5.5_

- [x] 6. Implement the gateway
  - [x] 6.1 Write the mitmproxy addon: proxy auth (407 on missing/revoked credentials) in `http_connect` and `requestheaders`
    - _Requirements: 1.2, 2.1, 2.2, 2.3_
  - [x] 6.2 In `request`: build context, parse MCP, evaluate policy, return a 403 JSON response on deny, publish an event
    - _Requirements: 2.4, 2.5, 4.4, 3.1_
  - [x] 6.3 Handle `terminate_on_violation` by calling `RunManager.stop`
    - _Requirements: 5.4_
  - [x] 6.4 In `response`: attach status code and size to the event; return 502 events on upstream failure
    - _Requirements: 3.1_
  - [x] 6.5 Deny agent requests addressed to the control API
    - _Requirements: 9.4_
  - [x] 6.6 Write an integration test: stub upstream server, stub agent, assert allow, block with 403 body, events emitted, and that a stopped run's token is rejected
    - _Requirements: 2.2, 4.4, 5.2_

- [x] 7. Implement the control API
  - [x] 7.1 Build the FastAPI app with operator bearer-token auth, localhost binding, `/health`
    - _Requirements: 9.1, 9.2_
  - [x] 7.2 Add run endpoints: register, finish, list (with allowed/blocked counts), events query, stop
    - _Requirements: 1.3, 1.4, 5.1, 6.2, 7.1_
  - [x] 7.3 Add policy endpoints: get, reload, replace (validated)
    - _Requirements: 4.5, 4.6_
  - [x] 7.4 Add the WebSocket `/ws` event stream with optional `run_id` filter
    - _Requirements: 3.2_
  - [x] 7.5 Write API tests, including auth rejection
    - _Requirements: 9.1_

- [x] 8. Implement the backend entrypoint
  - Start the mitmproxy `DumpMaster` and uvicorn on the same asyncio loop, print the operator token once, and expose the mitmproxy CA path
  - Add a policy file watcher for hot reload
  - _Requirements: 2.1, 4.5, 9.2_

- [x] 9. Implement the launcher CLI
  - [x] 9.1 `harness run -- <cmd>`: health check, register run, build proxy and CA env vars, spawn in a new session, stream output, report exit code
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 9.2 `harness tail <run-id>`: stream colored events from the WebSocket
    - _Requirements: 3.5_
  - [x] 9.3 `harness stop <run-id>` and Ctrl+C forwarding
    - _Requirements: 5.1_
  - [x] 9.4 Write an end-to-end test: launch a stub agent through the CLI and verify the run lifecycle
    - _Requirements: 1.1, 1.4_

- [ ] 10. Checkpoint: demo path works headless
  - Run the stack locally, launch a stub agent, confirm traffic is logged, a disallowed request is blocked, and stop kills the agent. Ask the user if questions arise.

- [ ] 11. Build the demo agent
  - [ ] 11.1 Write `demo/demo_agent.py`: calls Bedrock via boto3 `converse`/`converse_stream` in `us-east-1` using env credentials and `BEDROCK_MODEL_ID` (allowed), GitHub (allowed), then a disallowed action (a different Bedrock model, a non-allowlisted host, and/or MCP `delete_*` tool call), with pauses so the demo is readable
    - _Requirements: 8.1_
  - [ ] 11.2 Add an optional mode that runs Kiro CLI as the managed agent, documenting any extra env/CA setup
    - _Requirements: 8.1_

- [ ] 12. Build the React dashboard
  - [ ] 12.1 Set up the API client, operator token entry, and the `useEventStream` hook with reconnect
    - _Requirements: 3.2, 7.4_
  - [ ] 12.2 Runs sidebar with status, command, allowed/blocked counters, and a Stop button
    - _Requirements: 7.1, 7.2, 5.1_
  - [ ] 12.3 Live traffic table with color and icon/text status, decision filter, auto-scroll toggle
    - _Requirements: 3.2, 3.3_
  - [ ] 12.4 Event detail drawer with redacted headers and matched rule
    - _Requirements: 3.4_
  - [ ] 12.5 Policy panel with the blocking rule highlighted and a reload button
    - _Requirements: 7.3, 4.5_
  - [ ] 12.6 Accessibility pass: semantic table, keyboard navigation, labels, contrast
    - _Requirements: 7.5_
  - [ ] 12.7 Component tests for the event table and stop button
    - _Requirements: 7.2, 3.3_

- [ ] 13. Add AWS integrations
  - [ ] 13.1 Implement the DynamoDB `AuditStore` (pk `run_id`, sk `ts#event_id`) and select it by config, falling back to SQLite
    - _Requirements: 6.4_
  - [ ] 13.2 Confirm Bedrock works through the proxy
    - Verify model access in the Bedrock console (Model catalog, region us-east-1) and copy the inference profile ID if one is shown
    - Run a standalone `converse` call first (no proxy), then through the harness; confirm the allow rule and that `converse_stream` is not buffered
    - Add a policy rule that allows only the chosen model path and confirm another model is blocked with a 403
    - _Requirements: 8.1, 4.1, 4.4_
  - [ ] 13.2a Extend redaction to mask `X-Amz-Security-Token` and SigV4 signatures
    - _Requirements: 6.3, 9.3_
  - [ ] 13.3 Emit CloudWatch metrics (allowed, blocked, stops) when configured
    - _Requirements: 6.1_
  - [ ] 13.4 Stretch: deploy the backend to ECS Fargate or EC2 and document the architecture
    - _Requirements: 8.3_

- [ ] 14. Documentation and submission assets
  - [ ] 14.1 Write the README: setup, architecture diagram, demo script, AWS services used, known bypass limits and mitigations, business case (who buys this and why)
    - _Requirements: 8.3, 9.5_
  - [ ] 14.2 Add `make demo` that runs the scripted flow and asserts the final state
    - _Requirements: 8.4_
  - [ ] 14.3 Capture screenshots, record a short video demo, and make a logo and slides for Devpost
    - _Requirements: 8.3_

- [ ] 15. Final checkpoint
  - Run the full test suite and a clean-machine rehearsal of the demo (launch, traffic, block, stop) in under 3 minutes. Ensure all tests pass, ask the user if questions arise.
