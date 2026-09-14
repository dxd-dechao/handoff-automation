# Coding task profile v1

URI: `urn:handoff-automation:coding-task:v1`

This is the provider-neutral A2A contract used by the optional `handoff-a2a` development path. The existing `bin/handoff` CLI is unchanged in A1 and does not yet call this profile.

## Preconditions (A1)

A1 does **not** accept an `approved: true` boolean as proof of human approval. The experimental path treats these as sufficient preconditions:

- the local HANDOFF status is `READY FOR EXECUTION` or `CHANGES REQUESTED`
- the submitted snapshot bytes exactly match the workspace `HANDOFF.md` (and `request_sha256`)
- the caller presented a valid local bearer credential
- a developer helper (`handoff-a2a execute` or the library) was invoked explicitly

Persistent human-approval receipts and max-round enforcement arrive in A3. Do not treat A2A `COMPLETED` as Planner `APPROVED`.

## SendMessage data part

JSON object, `application/json`, no provider-specific fields and no model credentials:

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Profile URI above |
| `workflow_id` | string | Caller-generated identifier for the human task; reused across related helper calls |
| `run_id` | string | Distinct local audit identifier for this execution |
| `execution_id` | UUID string | One execution attempt, created before submission |
| `iteration` | positive integer | Initial execution is `1`; each correction execution increments |
| `workspace_id` | string | Configured logical workspace name, never a client-selected absolute server path |
| `expected_branch` | string | Must equal the HANDOFF `Branch` field and the checked-out branch |
| `expected_head` | string | Must equal the workspace `HEAD` at validation time |
| `handoff_markdown` | string | Full UTF-8 HANDOFF.md snapshot (plan, execution notes, QA feedback) |
| `request_sha256` | hex | SHA-256 of those exact UTF-8 snapshot bytes |

Transport: official SDK `SendMessage` with `returnImmediately: true`, then `GetTask` polling. Each correction is a **new** A2A task. Server task/context IDs are opaque. Match returned `run_id` / `execution_id` / `task_id` before using a result.

## Coding-result artifact

Artifact name: `coding-result`. Same profile URI in `schema`.

Notable fields: `summary`, `execution_notes`, `exit_code`, `provider_error`, `invalid_output`, `invalid_handoff_transition`, `reason`, branch/HEAD before and after, `commits`, `git_status`, `diff`, `duration_s`, `usage` (nullable integer counts), `cost_usd` (nullable; `0` is a real value), `usage_provenance`, `cost_provenance`, `evidence_dir`, `plan_intact`, `worker_launched`.

Unknown costs stay `null`. Valid false/zero values are preserved. Missing costs are never invented.

## Task states

| Outcome | A2A state | When |
|---|---|---|
| Delivery succeeded | `COMPLETED` | Process exit 0, valid provider JSON without `is_error`, HANDOFF transitioned to `READY FOR QA`, Planner-owned plan/QA sections intact |
| Worker ran but delivery failed | `FAILED` | Nonzero exit, provider `is_error` even with exit 0, malformed output, or invalid HANDOFF transition (including an executor writing `APPROVED`) |
| Refused before worker | `REJECTED` | Bad request/schema, wrong workspace, snapshot/hash mismatch, ineligible status, branch mismatch, stale HEAD, or busy execute lock |
| Cancel | JSON-RPC error | A1 returns not-cancelable/unsupported; it does not mark a running worker canceled |

`COMPLETED` means the execution was delivered for independent Planner QA. It is not a claim that the code is correct.

## Server limitations (A1)

- Loopback bind only (`127.0.0.1`, `::1`, `localhost`)
- One registered workspace
- In-memory SDK task storage; tasks do not resume after process restart
- Execute lock is `.handoff-logs/execute.lock` (directory); a busy workspace is refused; unexplained locks are never deleted
- The server never writes a different remote plan over a mismatched local `HANDOFF.md`
- The adapter launches the configured binary with `-p "execute the handoff"`, `--permission-mode acceptEdits`, configured `--model`, and `--output-format json`. Inherited `ANTHROPIC_*` / `CLAUDE*` variables are stripped. The service bearer is not passed to the child
- No automatic resubmission or fallback to `handoff execute`
- On a lost connection after submit, report the known task ID or unresolved `execution_id`

## Client limitations (A1)

- Provider-neutral: no Claude/Codex flags in the request
- No arbitrary instruction/prompt CLI argument
- CLI integration into `bin/handoff` is A3
