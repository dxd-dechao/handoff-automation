# Coding task profile v1

URI: `urn:handoff-automation:coding-task:v1`

This is the provider-neutral A2A contract used by `handoff-a2a` and, when a
target repo sets `transport: a2a`, by `bin/handoff`. The legacy direct-Claude
path remains the default when `.handoff-config.json` is absent.

## Preconditions

A1/A2 development helpers still treat these as sufficient preconditions:

- the local HANDOFF status is `READY FOR EXECUTION` or `CHANGES REQUESTED`
- the submitted snapshot bytes exactly match the workspace `HANDOFF.md` (and `request_sha256`)
- the caller presented a valid local bearer credential
- a developer helper (`handoff-a2a execute` or the library) was invoked explicitly

The integrated CLI (`handoff approve` / `handoff execute`) additionally requires
a local approval receipt, matching plan hash, registered workspace/branch, an
available round, a clean or continuation code fingerprint, and an Agent Card that
advertises both `durable_execution_id_deduplication` and
`workspace_code_fingerprint`. Do not treat A2A `COMPLETED` as Planner `APPROVED`.

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
| `expected_code_fingerprint` | hex, optional | Additive. SHA-256 of branch/HEAD plus tracked diffs and nonignored untracked code, excluding known local workflow files. Required by the integrated CLI. The A2 development helper may omit it. When present, the server verifies it under the execute lock before spawning. |

Transport: official SDK `SendMessage` with `returnImmediately: true`, then `GetTask` polling. Each correction is a **new** A2A task. Server task/context IDs are opaque. Match returned `run_id` / `execution_id` / `task_id` before using a result.

## Coding-result artifact

Artifact name: `coding-result`. Same profile URI in `schema`.

Notable fields: `summary`, `execution_notes`, `exit_code`, `provider_error`, `invalid_output`, `invalid_handoff_transition`, `reason`, branch/HEAD before and after, `commits`, `git_status`, `diff`, `duration_s`, `usage` (nullable integer counts), `cost_usd` (nullable; `0` is a real value), `usage_provenance`, `cost_provenance`, `evidence_dir`, `plan_intact`, `worker_launched`, and (additive) executor identity: `executor_provider`, `executor_model` (requested), `executor_reported_model` (what the provider reported, or `null`), `config_generation` (managed selection number, or `null`). Requested and reported identities are kept separate; the client never branches on them.

Unknown costs stay `null`. Valid false/zero values are preserved. Missing costs are never invented.

## Task states

| Outcome | A2A state | When |
|---|---|---|
| Delivery succeeded | `COMPLETED` | Process exit 0, valid provider JSON without `is_error`, HANDOFF transitioned to `READY FOR QA`, Planner-owned plan/QA sections intact |
| Worker ran but delivery failed | `FAILED` | Nonzero exit, a provider-reported error even with exit 0 (Claude `is_error`, Codex `turn.failed`, Cursor `result.is_error`/non-`success` subtype), malformed output (including Cursor stream-json without a terminal `result` event), or invalid HANDOFF transition (including an executor writing `APPROVED`) |
| Refused before worker | `REJECTED` | Bad request/schema, wrong workspace, snapshot/hash mismatch, ineligible status, branch mismatch, stale HEAD, or busy execute lock |
| Cancel after confirmed stop | `CANCELED` | CancelTask stopped the owned process group and captured partial evidence |
| Server deadline | `FAILED` | `execution_timeout_s` elapsed; worker stopped; deadline reason recorded |
| Uncertain ownership / failed stop | `INPUT_REQUIRED` | `recovery_required` metadata; workspace reservation retained |

`COMPLETED` means the execution was delivered for independent Planner QA. It is not a claim that the code is correct.

The integrated CLI moves Markdown to `READY FOR QA` only after a valid `COMPLETED` result. For a stopped `FAILED` or `CANCELED` execution, it restores the Status line from the submitted snapshot. Any Executor-written `READY FOR QA`/`APPROVED` is recorded as `executor_status_discarded`; Execution Notes and code are kept. `handoff status` then routes to Planner review. The resulting `dispatch_hold` survives transport/config changes. Neither A2A nor legacy `handoff watch` dispatches past it; only an explicit `handoff execute` clears it.

## Durable identity (A2)

- Deduplication key is trusted server `caller_id` + `execution_id`
- The server hashes the complete validated coding request, not only `request_sha256`
- Identical retransmission reuses the original task/context/run identity
- A different request body under the same `execution_id` is refused
- Agent Card extension `params.durable_execution_id_deduplication` is `true` on this server
- Agent Card extension `params.workspace_code_fingerprint` is `true` on this server
- Client run records are written atomically before SendMessage; resume retransmits only that saved request
- A1-era disk evidence without a database row is not imported as a resumable task
- Artifact `worker-lifecycle` reports `worker_started` / `worker_stopped` / `lock_held` for reconciliation

## Server limitations

- Loopback bind only (`127.0.0.1`, `::1`, `localhost`)
- One registered workspace and one server process per database
- SQLite task/execution store (`state_db`, default `evidence_dir/state.sqlite`)
- Optional config: `caller_id` (default `local-planner`), `execution_timeout_s` (default 3600), `cancel_grace_s` (default 5)
- Execute lock is `.handoff-logs/execute.lock` (directory) plus `owner.json` when this server holds it. Unexplained locks are never deleted
- The worker is launched in a POSIX process group. Cancel, deadline, and shutdown share one stop/reap path before lock release
- Worker launch happens behind a spawn gate that the launch thread holds while it records ownership. Cancel, deadline, shutdown, and any lock release first close the gate, which waits out an in-flight launch. A cancel that wins before launch therefore prevents it. A worker that did launch is owned, stopped, and reaped before the lock is released, even if the SDK cancels the awaiting coroutine. `worker_started` reflects whether a process actually launched
- After restart, identifiable owned workers are stopped and marked `FAILED`. Uncertain identity stays reserved with `recovery_required`
- The server never writes a different remote plan over a mismatched local `HANDOFF.md`
- Exactly one adapter per server (`claude`, `codex`, or `cursor` block). The server owns spawn, process group, deadlines, cancel, locks, and evidence for all of them. Each adapter supplies only argv, the child environment, and output interpretation (`adapters/base.py`), plus optional per-run delivery files (`prepare_run`/`cleanup_run`) that the server creates after validation under the execute lock and removes once the worker is confirmed stopped (also on cancel, deadline, and restart reconciliation)
- Claude: the configured binary with `-p "execute the handoff"`, `--permission-mode acceptEdits`, configured `--model`, and `--output-format json`. Inherited `ANTHROPIC_*` / `CLAUDE*` / `CURSOR_*` variables are stripped (`CURSOR_*` covers a Cursor Planner shell that started the server)
- Codex (verified with codex-cli 0.156.0): `exec --json --ignore-user-config --model M -c <handoff permission profile> -c default_permissions="handoff" -c approval_policy="never" --cd <workspace> "execute the handoff"`, plus optional `model_reasoning_effort`. The validated snapshot is passed as `developer_instructions` behind a one-line provenance header, because HANDOFF.md is git-excluded and hidden from Codex's file search. Codex adds it alongside the repo's AGENTS.md / AGENTS.override.md. User-scope skills are disabled per run. The profile extends `:workspace` with `.git` write (the built-in profile blocks `git commit`); network stays off. Inherited `OPENAI_*`, `AZURE_OPENAI_*`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `ANTHROPIC_*`, `CLAUDE*`, and `CURSOR_*` are stripped. JSONL `turn.completed.usage` maps to neutral usage: `cached_input_tokens` → `cache_read_input_tokens` (Codex `input_tokens` includes cached tokens), and `cache_write_input_tokens` → `cache_creation_input_tokens` (`null` when not reported). There is no cost (`null`). `turn.failed`, or `error` without a completed turn, is a provider error
- Cursor (verified against cursor-agent 2026.09.18-9a7762b): `--print --output-format stream-json --model M --workspace <repo> --trust --force --sandbox enabled "execute the handoff"`. The validated snapshot is delivered as `.cursor/rules/handoff-executor-<execution_id>.mdc` (`alwaysApply: true`, a marker line, the execution ID, a short Executor-role statement, a one-line provenance header, then the verbatim HANDOFF). Its path is added to the local git exclude, a tracked or pre-existing file at that path refuses delivery, and cleanup removes only a file carrying the marker. `CURSOR_CONFIG_DIR` points at a per-run `cli-config.json` (deny `git push/merge/remote`, `gh`, `handoff`, `handoff-a2a`, nested agents, `.git/config` and hook writes; sandbox enabled with an empty network allowlist). Inherited `CURSOR_*`, `ANTHROPIC_*`, `CLAUDE*`, `OPENAI_*`, `AZURE_OPENAI_*`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN` are stripped; an optional server-side `api_key_file` is passed only as `CURSOR_API_KEY` in the child environment. The `system/init` event's `model` is recorded as `executor_reported_model`. Usage is taken from `result.usage` when present (camelCase or snake_case counts); there is no cost (`null`)
- Agent Card `name` identifies the adapter (`handoff-a2a Claude Executor` / `Codex` / `Cursor`). Profile params add `executor_provider`, `executor_model`, `workspace_id`, and (managed configurations) `config_generation`. The coding contract never branches on them; the managed CLI compares them with its selection before dispatch and before trusting a running service
- The service bearer is not passed to the child
- No automatic resubmission or fallback to `handoff execute`
- Historical A1 in-memory tasks do not survive into this store

## Client limitations

- Provider-neutral: no provider flags in the request; the same client and orchestration drive every adapter
- Managed repos: `execute`/`watch` first apply a queued selection (only when no run is outstanding), then, under `submit.lock`, snapshot the configuration, fetch the Agent Card, and require its `workspace_id`/`config_generation`/provider/model to match the selection before recording the run. Each run records the identity it was submitted to (`outstanding.executor`, run record, manifest `executor`), which later selection changes never rewrite
- An outstanding run's saved endpoint, request, and credential reference stay authoritative for `status`/`resume`/`cancel` after `.handoff-config.json` changes or is removed
- No arbitrary instruction/prompt CLI argument
- `handoff-a2a status|resume|cancel --run-record ... --credential-file ...` are development helpers. Client `--timeout` is wait time only and does not cancel the worker
- Exit codes for execute/resume: `0` successful `COMPLETED`, `1` unsuccessful terminal, `2` unresolved / `INPUT_REQUIRED`
- Resume with an unknown task ID retransmits only if the card advertises durable execution-ID deduplication
- Production `handoff` A2A mode calls `handoff-a2a cli` and refuses an endpoint that cannot enforce `workspace_code_fingerprint`
