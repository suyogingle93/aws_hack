# Requirements Document

## Introduction

Agent Harness is a control layer for desktop and CLI-based AI agents (Track 2: "Secure and Manage Agent Activity"). Instead of letting an agent talk directly to the network, models, and tools, the harness launches the agent as a managed process and routes all of its outbound requests through a central gateway. The gateway records traffic, evaluates policy, blocks disallowed actions, and can stop a run on demand.

The hackathon demo must show, end to end: launch a CLI agent, display its traffic live, block a disallowed action, and stop the run.

Assumptions (please correct any that are wrong):
- The primary target agent is a CLI agent (for example Kiro CLI or a simple scripted agent). Desktop agents are supported on a best-effort basis through proxy settings.
- The gateway is an HTTP(S) forward proxy plus an MCP/tool-call inspection path, running locally or on AWS.
- AWS services are used where they add value (Bedrock for the demo agent's model, DynamoDB for audit storage, CloudWatch for metrics, optionally Verified Permissions/Cedar for policy).

## Glossary

- **Harness**: The overall system (CLI launcher, gateway, policy engine, audit store, dashboard).
- **Gateway**: The central service every managed agent request passes through.
- **Managed_Agent**: An agent process launched and supervised by the Harness.
- **Run**: One lifecycle of a Managed_Agent from launch to exit or stop.
- **Policy**: A set of allow/deny rules evaluated by the Gateway for each request.
- **Policy_Engine**: The component that evaluates a request against the active Policy.
- **Audit_Store**: Persistent storage of request/decision events.
- **Dashboard**: Web UI showing live traffic, decisions, and run controls.
- **Launcher**: The CLI (`harness run -- <command>`) that starts a Managed_Agent.

## Requirements

### Requirement 1: Launch an agent under management

**User Story:** As a security operator, I want to launch a CLI agent through the Harness, so that all its activity is governed from the start.

#### Acceptance Criteria

1. WHEN the user runs `harness run -- <command>`, THE Launcher SHALL start the command as a child process and create a Run with a unique run ID.
2. WHEN the Launcher starts a Managed_Agent, THE Launcher SHALL configure the child environment (for example `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and a custom CA bundle variable) so outbound traffic goes through the Gateway.
3. WHEN a Run starts, THE Launcher SHALL register the Run with the Gateway, including run ID, command, working directory, and start time.
4. WHEN the Managed_Agent exits, THE Launcher SHALL mark the Run as finished and record its exit code.
5. IF the Gateway is unreachable at launch, THEN THE Launcher SHALL refuse to start the agent and print an actionable error.

### Requirement 2: Central gateway routing

**User Story:** As a security operator, I want all agent requests to pass through one gateway, so that I have a single point of visibility and control.

#### Acceptance Criteria

1. THE Gateway SHALL accept HTTP and HTTPS (CONNECT) proxy traffic from Managed_Agents.
2. WHEN a request arrives, THE Gateway SHALL associate it with a Run using a per-run credential supplied by the Launcher.
3. IF a request carries no valid run credential, THEN THE Gateway SHALL reject it and log the rejection.
4. THE Gateway SHALL forward allowed requests to their destination and return the response to the Managed_Agent.
5. WHEN the Gateway recognizes an MCP or JSON-RPC tool call, THE Gateway SHALL parse the tool name and arguments for policy evaluation.

### Requirement 3: Live traffic visibility

**User Story:** As a security operator, I want to see what the agent is doing in real time, so that I can understand and trust its behavior.

#### Acceptance Criteria

1. WHEN the Gateway handles a request, THE Gateway SHALL emit an event containing run ID, timestamp, method, destination host, path, tool name (if any), decision, and matched rule.
2. THE Dashboard SHALL display events for the selected Run within 2 seconds of occurrence.
3. THE Dashboard SHALL visually distinguish allowed, blocked, and pending events.
4. WHEN the user selects an event, THE Dashboard SHALL show request details with sensitive header values redacted.
5. THE Launcher SHALL offer a `harness tail <run-id>` command that streams the same events in the terminal.

### Requirement 4: Policy enforcement and blocking

**User Story:** As a security operator, I want to define what an agent may do, so that disallowed actions are blocked automatically.

#### Acceptance Criteria

1. THE Policy_Engine SHALL support rules matching on destination host, URL path, HTTP method, and MCP tool name.
2. THE Policy_Engine SHALL support allow and deny actions, with deny taking precedence over allow.
3. WHEN no rule matches a request, THE Policy_Engine SHALL apply a configurable default action (default: deny).
4. WHEN a request is denied, THE Gateway SHALL not forward it, SHALL return a clear 403 response with the reason to the Managed_Agent, and SHALL log a blocked event.
5. THE Policy SHALL be loadable from a human-readable file (YAML or JSON) and reloadable without restarting the Gateway.
6. WHEN the Policy changes mid-run, THE Gateway SHALL apply the new Policy to subsequent requests.
7. IF a request exceeds a configurable rate limit for a Run, THEN THE Gateway SHALL block it and log the reason.

### Requirement 5: Stop a run (kill switch)

**User Story:** As a security operator, I want to stop a misbehaving agent immediately, so that it cannot continue causing harm.

#### Acceptance Criteria

1. WHEN the operator triggers Stop from the Dashboard or `harness stop <run-id>`, THE Harness SHALL terminate the Managed_Agent process tree.
2. WHEN a Run is stopped, THE Gateway SHALL revoke the run credential and reject all further requests from that Run.
3. WHEN a Run is stopped, THE Harness SHALL record a stop event with actor and reason.
4. WHERE a policy rule is marked `terminate_on_violation`, THE Harness SHALL stop the Run automatically when that rule is triggered.
5. THE Harness SHALL complete a stop within 3 seconds of the request under normal conditions.

### Requirement 6: Audit and persistence

**User Story:** As a compliance reviewer, I want a durable record of agent activity, so that I can review what happened after the fact.

#### Acceptance Criteria

1. THE Harness SHALL persist all events and Run metadata to the Audit_Store.
2. THE Audit_Store SHALL support querying events by run ID and time range.
3. THE Harness SHALL redact secrets (authorization headers, API keys, tokens) before persistence.
4. WHERE AWS is configured, THE Audit_Store SHALL use DynamoDB; OTHERWISE THE Audit_Store SHALL fall back to local SQLite or JSONL storage.

### Requirement 7: Dashboard and operator experience

**User Story:** As a judge or operator, I want a polished UI, so that I can quickly understand and control agent activity.

#### Acceptance Criteria

1. THE Dashboard SHALL list active and past Runs with status, command, and counts of allowed and blocked requests.
2. THE Dashboard SHALL provide a Stop button for each active Run.
3. THE Dashboard SHALL provide a policy view showing active rules and highlighting the rule that blocked a given event.
4. THE Dashboard SHALL be usable without manual page refresh.
5. THE Dashboard SHALL meet basic accessibility practices (keyboard navigation, sufficient contrast, labelled controls).

### Requirement 8: Demo readiness

**User Story:** As a hackathon team, I want a reliable scripted demo, so that judges see the full flow in under 3 minutes.

#### Acceptance Criteria

1. THE Harness SHALL include a demo agent (or demo script for a CLI agent) that makes both allowed and disallowed requests.
2. THE Harness SHALL include a sample Policy that blocks at least one demo action (for example a request to a non-allowlisted host or a dangerous MCP tool).
3. THE repository SHALL include a README with setup steps, architecture diagram, demo script, and AWS services used.
4. THE demo flow SHALL be: launch agent, show live traffic, block a disallowed action, stop the run.

### Requirement 9: Security of the harness itself

**User Story:** As a security operator, I want the harness to be hard to bypass or abuse.

#### Acceptance Criteria

1. THE Gateway control API (stop, policy reload) SHALL require an authentication token.
2. THE Gateway SHALL bind to localhost by default unless explicitly configured otherwise.
3. THE Harness SHALL NOT log secret values in plaintext.
4. IF the Managed_Agent attempts to reach the control API, THEN THE Gateway SHALL deny the request.
5. THE Harness documentation SHALL state known bypass limits (for example agents that ignore proxy settings) and the mitigations used.

### Non-Goals (for the hackathon)

- Full OS-level sandboxing or kernel enforcement (documented as future work).
- Multi-tenant SaaS user management.
- Deep content inspection of encrypted traffic beyond what the local CA permits.
