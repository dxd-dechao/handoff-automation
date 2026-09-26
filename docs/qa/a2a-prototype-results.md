# A2A prototype evidence

Verified locally on 2026-09-14 against repository baseline `6201590`.

The temporary prototype used official `a2a-sdk==1.1.2` with A2A 1.0 JSON-RPC. One unchanged client sent identical temperature-conversion tasks to Claude Code and Codex endpoints. Initial and follow-up instructions matched; both endpoints started at the same git HEAD. Independent numerical tests and direct git-diff inspection passed for both.

| Path | Initial | Follow-up | Result |
|---|---:|---:|---|
| Existing direct Claude CLI | 25 s | Not run | Coding checks passed |
| A2A → Claude Code | 20.402 s | 15.859 s | Both rounds passed |
| A2A → Codex | 33.516 s | 24.481 s | Both rounds passed |

These single observations are feasibility evidence, not evidence of a performance or quality improvement. The follow-up deliberately added a named test; it was not a discovered implementation defect. The baseline did not exercise a full HANDOFF.md transition loop.

Final fake-worker probes passed 10/10: unauthorized request, missing approval flag, stale HEAD, duplicate task identity, workspace conflict, reconnect after acknowledgement, cancellation terminal state, stopped direct worker, lock release, and independent QA rejecting a false completion.

The first cancellation test failed because process-group termination was denied. Direct-child termination passed after adjustment; cancellation of real worker descendants remained untested. Task state/deduplication were in memory; restart recovery and lost-ack recovery were not tested. Approval was a fixture flag, not actual human authorization. Both endpoints shared the server implementation. The prototype did not integrate the production CLI or enforce production iteration limits.

Current repository smoke tests passed 17/17. Additional probes confirmed existing prompt discipline, auth-variable stripping, status refusal and locking, plus gaps: execution can start without a separate approval receipt; a fourth correction run is accepted; a provider error with subprocess exit zero is not converted to command failure.

Reference files available on the original machine at planning time:

- `/private/tmp/handoff-a2a-prototype/REPORT.md`
- `/private/tmp/handoff-a2a-prototype/server.py`
- `/private/tmp/handoff-a2a-prototype/client.py`
- `/private/tmp/handoff-a2a-prototype/run_experiment.py`
- `/private/tmp/handoff-a2a-prototype/runs/20260914-231348-live-157cd8/summary.json`
- `/private/tmp/handoff-a2a-prototype/runs/20260914-231614-stub-808138/summary.json`

These paths are temporary and optional references, not dependencies of implementation or tests. Rebuild the package and fixtures in this repository; do not copy credentials, temporary run tokens, virtual environments, or machine-specific paths into the product.

Primary protocol/runtime references: [A2A specification](https://a2a-protocol.org/latest/specification/), [official Python SDK](https://github.com/a2aproject/a2a-python), [Codex non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode).
