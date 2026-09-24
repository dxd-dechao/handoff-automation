## Current Task

**Status:** DRAFT

**Branch:** codex/a6-cli-setup-cursor-model-switch

**Task ID:** A6-CLI-USABILITY-CURSOR

**Predecessor:** A5-CANCEL-STARTUP, APPROVED at 1e09ec5; preserved in HANDOFF-ARCHIVE.md. This is new product scope, not another A5 correction.

**Approval:** Planning requested on 2026-09-24. This draft is not execution approval. After explicit human approval, activate it for one manually launched long Executor session, followed by independent QA from the original Planner. Do not launch implementation from this planning task.

### Goal and user requirements

Make handoff usable without manually authoring JSON, add Cursor CLI/models as a supported Executor and Planner host, and let users change Executor provider/model through handoff during an ongoing workflow.

The user explicitly rejected the current setup and requested:
1. init creates and populates required JSON automatically.
2. Cursor models and CLI work as Executor and Planner.
3. Switching models midway is part of the CLI.

Completion means the public commands perform these operations end to end. README instructions to edit JSON, fabricated Cursor model names, a fake-only adapter, or a switch command that requires manually restarting servers do not satisfy the request.

### Verified baseline

- Repository: /Users/CHEN_Dechao/Documents/GitHub/handoff-automation.
- Planning HEAD: 7ee6d788d30b594292f2c8a18fd25c6930e0d128 (Update docs), on codex/a2a-executor-mvp. Tracked worktree was clean. Record the actual execution starting HEAD and preserve later/unrelated changes.
- Open PR check on 2026-09-24: none, including drafts.
- Latest independent regression record: 99 Python tests and 17 legacy checks at 1e09ec5; this planning turn did not rerun them.
- bin/handoff cmd_init only installs the protocol/exclusions/Claude permissions and returns early when HANDOFF.md exists. It does not generate A2A configuration. Top-level dispatch currently forwards only one repo argument for most verbs.
- Current legacy status reports DRAFT with turn UNKNOWN. Correct this touched user-facing status mapping to human plan review and preserve refusal to execute drafts.
- src/handoff_a2a/config.py reads .handoff-config.json; absent config means Python-free direct-Claude legacy execution.
- server.py loads exactly one Claude or Codex adapter at startup. CodingAgentExecutor retains that adapter. Editing a model string in a file does not change a running server.
- integration.py owns approve/execute/watch/reconciliation; workflow.py owns approval receipts, counts and holds; workspace.py owns locks and fingerprints. Saved run endpoint/credential references survive config changes.
- Cursor executables are installed at /Users/CHEN_Dechao/.local/bin/{agent,cursor-agent}. Local cursor-agent --version: 2026.09.18-9a7762b. Help confirms --model, models/--list-models, --workspace, --print, JSON/stream-json, --sandbox, --trust and --force. Probe commands ran in /private/tmp and made no model calls.
- cursor-agent models returned Authentication required. Account-specific Grok IDs, login validity for execution, output shape, instruction delivery and actual write/commit permissions have NOT been verified. Do not infer account access from the GUI model picker.
- Cursor's local --mode=plan describes a read-only mode. A Planner that must write HANDOFF.md needs an appropriate editable interactive session with role instructions; do not promise that native read-only plan mode writes the draft.
- No repository AGENTS.md or CLAUDE.md was found during planning. Read them if added before execution.
- The existing implementation checkout remains on its current configuration. Perform setup/worker experiments in disposable repositories, not by enabling the new backend in the repo currently implementing it.

### Intended user experience and command contract

These are commands TO IMPLEMENT, not commands already available. Preserve existing repo-first syntax and quote paths. The example model placeholder must be replaced with a model ID discovered for the selected provider.

```sh
# Guided first-time setup: choose transport/provider/model/Planner host.
handoff init "/path/to/project"

# Equivalent scripted setup; all configuration/token/skill files are generated.
handoff init "/path/to/project" --transport a2a --executor cursor --model "<available-model-id>" --planner cursor

handoff models "/path/to/project" --provider cursor
handoff model "/path/to/project"                         # selected / pending / active identities
handoff server start "/path/to/project"                  # starts and verifies generated local server
handoff server status "/path/to/project"

# Optional Cursor interactive Planner launcher; session remains human-facing.
handoff planner "/path/to/project" --provider cursor --model "<available-model-id>"

# After a self-contained DRAFT and explicit human approval:
handoff approve "/path/to/project"
handoff watch "/path/to/project"

# Between implementation and correction, within the same approved workflow:
handoff model "/path/to/project" --provider cursor --model "<another-available-model-id>"

# While a worker is running, explicitly schedule the next model:
handoff model "/path/to/project" --provider codex --model "<available-model-id>" --after-current

handoff status "/path/to/project"
handoff cancel "/path/to/project"                       # existing explicit cancellation path
handoff server stop "/path/to/project"                  # refuses while execution remains unresolved

# Compatibility path:
handoff init "/path/to/project" --transport legacy
```

Use these spellings consistently in help, tests, docs and skill. An optional --reasoning-effort flag may be offered only for adapters that support it. Do not overload model selection with arbitrary worker instructions.

New bare init is guided on a TTY, with A2A presented as the path for Claude/Codex/Cursor selection. A noninteractive new init without sufficient choices must print the complete flag-based command and exit without half-configuring a project. Never silently choose a paid provider/model. Existing initialized repos are preserved on bare re-init; explicit --transport a2a performs the migration. Missing-config legacy execute/status/runs remain usable without Python.

### Architecture decisions

Retain the provider-neutral A2A request/result contract and server-owned worker lifecycle. Add a small CLI setup/service/selection layer around the existing server. For CLI-managed local workspaces, manage one server endpoint and one durable database per workspace; change adapters by a verified idle stop/restart, preserving that database and audit trail. Do not introduce a second orchestration service or a live mutation of an active worker's adapter.

Keep old manually configured endpoints supported for execute/resume/status/cancel. Managed model/server commands must identify unmanaged endpoints and fail with a clear migration/setup command rather than changing an unrelated service. Migration must be explicit, refuse unresolved work, preserve a recoverable copy of existing settings, and not delete external credentials or databases.

Use the following managed layout:
- <repo>/.handoff-config.json: client routing, workspace identity, and versioned managed-service metadata referencing the generated server config.
- <repo>/.handoff-logs/server.json: selected Executor provider/model, resolved binary, loopback address, registered workspace, credential path, evidence path and state DB location.
- <repo>/.handoff-logs/service/: process ownership record, logs and any narrowly scoped switch transaction/pending-selection state.
- <repo>/.handoff-logs/credentials/service-token: generated local bearer token; never provider API credentials.
- <repo>/.handoff-logs/server-evidence/: durable server state/results.
- Project-scoped Cursor Planner skill and required Cursor local permission/instruction artifacts as described below.

Avoid duplicating the authoritative selected model between configs: server.json holds it; client metadata points to it. Active identity comes from the verified service/Agent Card; an in-flight run records its original identity. Any derived display/cache must be visibly distinguished from authoritative state.

### Phase 1 — Setup and provider discovery

Implement complete, idempotent configuration generation and inspectable readiness.

- Resolve the target Git worktree correctly, including .git files for linked worktrees; use Git plumbing for the local exclude location. Do not assume .git is always a directory.
- Resolve handoff-a2a through the existing override/PATH mechanism or this installation's known runtime; provide a precise setup command if unavailable. Do not silently download/install dependencies or log into accounts.
- Prompt only for real choices on a TTY. Explicit flags support unattended setup. Account auth/model discovery errors must be actionable and must not leave an apparently ready but unusable config.
- Generate and populate both JSON configs, a high-entropy local service token, and needed provider-local settings. Derive workspace ID, absolute paths, loopback port and defaults. Token file mode 0600; credential directory mode 0700. Never print the token, embed it in commands, or put it in reports.
- Setup must not launch a paid task or auto-approve the plan. Print the files created, provider/model, prerequisite status and exact next commands.
- Rerunning init must preserve HANDOFF content, tokens, current selection, approval/history and user settings. Changes require explicit CLI options; do not silently rotate tokens or overwrite malformed/custom/tracked files.
- Validate tracked/symlink collisions before sensitive writes. Use atomic writes and a setup/service mutex so partially generated files are not treated as ready. Roll back files created by a failed attempt where safe.
- Install only requested project-local Planner integration. For --planner cursor, merge/copy the repository-owned skill into .cursor/skills/handoff-cli without overwriting an unrelated skill. No global skill installation.
- Add precise local exclusions for generated files. Do not exclude all .cursor or .agents content or hide tracked user code from fingerprints. Integrate generated paths into workflow fingerprint handling.
- handoff models --provider cursor invokes the verified Cursor CLI discovery command, with version/auth errors and real IDs. Grok is supported through Cursor when returned/accepted by that account; no hardcoded assumed Grok catalog.
- For Claude/Codex, use a documented installed discovery interface where available; otherwise clearly report that enumeration is unavailable and accept an explicit provider-native ID, recording subsequent rejection honestly. Never label a guessed static list as account availability.
- Model/binary arguments must be treated as data, including bracketed model parameters and paths with spaces; no eval or shell string construction.

### Phase 2 — Managed local server commands

Implement handoff server start/status/stop for generated configurations.

- Start the existing serve implementation as a managed local child that outlives the short CLI command, with logs and verified process identity. Reuse the installed Python entry point; no OS-wide daemon/LaunchAgent.
- Start is idempotent only if the existing server matches the workspace, config generation and process identity. Use authenticated readiness plus Agent Card profile/provider/model checks. A matching TCP port alone is insufficient.
- Record PID and stronger start/ownership identity; never kill an arbitrary PID from a stale file. Port conflicts produce actionable errors, with no termination of unrelated listeners.
- Keep the same configured database through model changes. Stop refuses outstanding/pending-submission/working/recovery-required states, and never releases a worker lock speculatively.
- Serialize start/stop/switch against each other and submission. State the lock order in code/tests. Avoid waiting for worker completion while holding a lock reconciliation requires.
- Launch failure/timeouts leave the prior selection recoverable and prevent watch from dispatching to an unverified service.
- init generates configuration; server start starts the service; watch drives approved execution. Do not silently turn watch into the Planner or auto-start arbitrary external services.

### Phase 3 — Cursor Executor adapter

Add adapters/cursor.py and extend the existing adapter factory/configuration to exactly one of Claude, Codex or Cursor.

- Resolve cursor-agent (or verified Cursor agent alias), record version, and verify the installed CLI's actual flags. Use fresh headless sessions for each Executor run and the explicit configured model; no automatic resumption of a Planner session.
- Reuse server.py/processes.py ownership, A5 spawn gate, deadline, cancellation and durable reconciliation logic. Do not implement a separate unmanaged subprocess path.
- Deterministically deliver the complete validated HANDOFF snapshot alongside applicable repository guidance, including when HANDOFF is git-excluded and AGENTS.md/.cursor/rules exist. The worker's user prompt remains execute the handoff. A provider-specific temporary instruction/rule artifact is acceptable if collision-safe, locally excluded, lifetime-owned, restored/removed safely and proven loaded. Do not overwrite user AGENTS.md/rules.
- Inspect the Cursor instruction mechanism before choosing it; document the native mechanism and validate the actual received task. Do not assume Claude or Codex instruction flags work in Cursor.
- Ensure the installed Planner skill cannot intercept Executor invocation and recursively call handoff execute/watch. Cover that with the Planner skill installed in the same fixture.
- Use Cursor's native sandbox/permissions for the needed local edits, tests and commits. Verify practical no-push/no-merge/no-PR behavior with harmless denied-operation checks. Never assume Claude's allowlist controls Cursor. If a native --force flag is required for print-mode writes, pair it with verified explicit restrictions; do not silently grant unrestricted shell/network access.
- Preserve existing project settings and human interaction for the Planner. Do not change global Cursor configuration. Avoid approving all MCP servers or loading unrelated tools solely to make the worker run.
- Use the executor's intended stored login or explicit server-side credential reference supported by the installed CLI. Strip inherited Planner/provider credentials/selectors; keep A2A service credentials out of the child environment. Secrets must never enter argv or evidence.
- Parse actual Cursor structured output and terminal errors. Nonzero exit, provider error despite zero exit, malformed/truncated output or invalid HANDOFF delivery must not become success. Preserve legitimate zero usage; absent cost/tokens remain null with provenance.
- Persist requested provider/model and any actually reported identity separately. Availability in the Cursor UI is not evidence of CLI availability.
- Unsupported Cursor versions or unavailable models must fail visibly before mutation where detectable; no silent fallback to another model/provider.

### Phase 4 — Cursor as Planner, independent of Executor

Make Cursor editor and Cursor CLI first-class human-facing Planner entry points.

- --planner cursor installs the same provider-neutral handoff skill locally, with role guidance for plan, human approval, manual or watch dispatch, independent diff/tests QA, correction limits and human merge.
- handoff planner --provider cursor --model launches an interactive Cursor CLI in the correct repo with the requested model and Planner guidance. It does not launch an Executor, approve a draft, or interpret the Planner's exit as approval.
- Use an editable interactive mode when writing HANDOFF is required; native Cursor read-only plan mode may be described as an analysis step only. Keep the same Planner session for later QA where supported; do not build autonomous Planner orchestration.
- Planner and Executor selections are independent. Launching/changing a Cursor Planner model never rewrites the Executor config or a running task.
- Document editor model-picker selection and actual skill invocation syntax; verify the skill is discovered in the editor, rather than assuming metadata presence proves loading.
- Demonstrate a Cursor CLI Planner drafting a concrete DRAFT, waiting at approval, then reviewing a controlled diff and writing actionable QA without recursive dispatch or self-approval. Exercise with Executor configuration pointing to another provider to prove role independence.
- Editor discovery/invocation can be verified through the visible app if available, otherwise supply a precise manual acceptance step and mark it pending. Do not mark the entire Cursor Planner requirement passed if only CLI metadata was tested.

### Phase 5 — Model changes within a workflow

Implement handoff model inspection/selection and --after-current, integrated with watch.

- With no flags, show configured Executor, active service identity, current worker's recorded model and any pending selection. handoff status/runs must make the same distinctions.
- Between executions, --provider/--model validates the candidate, acquires the relevant coordination locks, verifies quiescence, restarts the owned service as needed, validates its identity and publishes the new config atomically. If the service is stopped, persist selection for the next start and report that state truthfully.
- Keep selected provider/model out of the task's plan hash unless the plan itself explicitly constrains them. A configuration-only switch preserves workflow ID, approved plan hash, round counts, holds, Git branch/HEAD and continuation fingerprint. If the HANDOFF contract specifies a model constraint, explain that changing that requirement needs a revised approved plan; never rewrite the contract implicitly.
- An immediate switch during outstanding submission, active work, unacknowledged transport, or recovery-required state must refuse with status/resume/cancel or --after-current guidance.
- --after-current records a validated pending selection without modifying the live service, current run or saved endpoint. Report exactly when it will apply. Repeating an identical request is idempotent; a newer explicit queued request replaces only the pending choice and records that change.
- watch, and the next explicit execute after reconciliation, applies the pending change once under the same coordination rules before a new worker can start. A pending change remains pending while ownership is uncertain.
- If the current run fails/is canceled, preserve dispatch_hold and require existing Planner review/explicit execute. Applying a selection never clears that hold or replenishes rounds.
- Never reinterpret switching as moving an in-progress conversation. If the user wants to interrupt, they explicitly cancel, reconcile, review partial Git changes and start a new execution under the same history rules.
- In-flight runs retain immutable provider/model/config generation and endpoint references in audit evidence, even after selection changes. New runs record the new identity.
- A failed candidate validation/start must retain or restore the last working configuration and prevent unintended dispatch. Preserve queued change/failure information for diagnosis.
- Fix the relevant stale-config race: cmd_execute_async currently reads config before taking submit_lock and rechecks without using the refreshed returned config. Select and snapshot the final effective configuration under coordination before reserving/submitting a run.
- Watch must reread effective selection at dispatch boundaries; test a running watcher through same-provider and cross-provider switches. Do not require the user to stop/restart watch or hand-edit JSON.

### Files in scope and implementation sequence

Keep Bash as the public interface and put substantive managed logic in small Python modules. The following paths are authorized after plan approval:

- bin/handoff: argument forwarding/help, setup/model/models/server/planner dispatch, legacy compatibility.
- src/handoff_a2a/{__main__,config,integration,workflow,workspace,reporting}.py: command entry points, managed metadata, locking, selection boundaries, pending selection, run evidence, generated-file handling.
- New src/handoff_a2a/{setup,service,selection,providers,planner}.py as focused helpers. Equivalent consolidation is allowed if it avoids unnecessary modules; record actual mapping.
- src/handoff_a2a/adapters/{base,__init__,cursor}.py and server.py: Cursor integration, config/identity; modify claude.py/codex.py only for shared setup/discovery/identity needs.
- src/handoff_a2a/{client,contracts,store,processes}.py only for concrete needs such as additive identity metadata, service ownership reuse or transaction support. Preserve backward reading of existing requests/results/state and keep worker ownership semantics.
- skills/handoff-cli/SKILL.md; templates/HANDOFF.md; narrowly scoped new Cursor/setup templates. Existing role instructions must be updated accurately for all three Executors and separate Planner selection.
- tests/a2a_harness.py; existing tests/test_a2a_{adapters,cli,contracts,lifecycle,processes,roundtrip,workflow}.py; new tests/test_a2a_{setup,service,selection,cursor_planner}.py.
- scripts/a2a_live_check.py or a dedicated scripts/a2a_cursor_live_check.py; README.md; docs/a2a-coding-task-v1.md; docs/product-status.md; docs/implementation-plan.md; new docs/cli-setup-and-models.md; docs/qa-a6-cli-usability.md and redacted durable evidence under docs/evidence/.
- pyproject.toml/uv.lock only if a demonstrated dependency need remains after considering the existing standard library/runtime. No general SDK/provider upgrades.

Suggested order: command/schema design and fixtures; setup/service primitives; Cursor adapter; Planner entry points; atomic switching/watch integration; focused tests; bounded native/live evidence; complete documentation and final regression. These are phases in one long execution, not separate Planner approval gates or recursively dispatched tasks. Commit coherent phases with resumable progress notes.

### Acceptance criteria

1. Fresh setup: a disposable Git repo reaches a verified ready local A2A service with the documented init/server commands and no hand-written JSON or token. Explicit Claude/Codex/Cursor selections generate valid files; re-init preserves task and secrets. Missing auth/runtime gives actionable truthful output.
2. Existing repos: bare re-init is nondestructive, old config endpoints remain supported, explicit migration is recoverable, paths with spaces and linked worktrees work, legacy execute/status/runs remain Python-free.
3. Service ownership: idempotent start, conflict-safe port handling, correct identity checks, refusal to stop unresolved work and failed-start recovery work through public commands. No unrelated process is stopped.
4. Cursor delivery: fake Cursor and a real available Cursor model edit/test/commit the approved tiny fixture and deliver READY FOR QA through handoff execute; full plan and repo instructions are demonstrably received, even with exclusions and the Planner skill present.
5. Three-provider regression: common success/failure/malformed output, cancellation/deadline, duplicate/reconnect and restart checks include Cursor. Immediate and deterministic in-flight cancellation show no delayed writes or surviving child after confirmed stop/lock release.
6. Planner independence: Cursor CLI loads the local skill, writes a DRAFT, respects approval, and performs independent QA of a supplied diff. Editor skill discovery/invocation is evidenced or specifically marked pending. Planner model changes leave Executor selection unchanged.
7. Immediate switching: a completed implementation followed by same-provider model change and cross-provider correction uses the selected identities while retaining workflow/approval/counts/Git state. No manual JSON/server/watch restart.
8. Queued switching: --after-current during a controlled slow worker leaves that worker on its original model, then the existing watch applies the new selection exactly once before the next eligible run. Include failure/cancel/unknown acknowledgement paths; holds and unresolved ownership still block dispatch.
9. Race/failure checks: concurrent switch vs submit/watch, failed new server start, stale process record and unsupported model do not produce duplicate workers, mislabel outcomes, lose the previous usable config or reset history.
10. Human-facing docs show the exact tested setup/Planner/watch/switch/recovery sequence and state remaining limitations. A feature cannot be reported fully accepted on fake-worker coverage alone.

### Verification and live-call budget

Use disposable repos/fake providers for development. Do not run setup, fake version probes or sample coding tasks in this implementation checkout. Track and clean up only processes/files created by each test.

Run focused behavioral tests after relevant edits; run the complete suite once after the final change:

```sh
bash -n bin/handoff
bash tests/smoke-manifest.sh
UV_CACHE_DIR=/private/tmp/handoff-qa-uv-cache uv run --offline --extra test python -m pytest -q
./bin/handoff --help
git diff --check
```

Keep the 17-check legacy smoke script intact; add separate meaningful tests for new behavior. Existing pytest collection only includes test_a2a_*.py, so use that prefix for all new modules. The server/process tests require localhost/process permissions; distinguish sandbox denial from product failures.

After this plan is explicitly approved, the future long execution includes a bounded opt-in native/live acceptance experiment, not default test behavior:
- Preflight Cursor authentication and available models without printing credentials. Use Grok if the account exposes a suitable exact ID; otherwise report Grok unavailable and use another available Cursor model for generic Cursor validation.
- At most six paid model turns total across disposable fixtures: Cursor implementation; Cursor different-model correction; cross-provider continuation using an already supported adapter; and up to three Cursor Planner turns covering status/draft/QA. Each sent model turn counts, including failures; no automatic retry after a failed delivery. Record the count and reason before each call.
- Do not rerun the broad A4 quality/cost comparison. Model switching is tested extensively with fake workers; use the small live sample to establish native input/write/output compatibility.
- Existing per-workflow three-execution limits remain in force. Use separate legitimate fixtures for separate acceptance cases, not fresh workflows to evade failed attempts.
- Provider login/account access remains user-owned. If required login/model access is absent, finish all independent implementation/tests/docs, record the exact pending acceptance and command, and return for the missing access. Do not fabricate a pass or silently reduce the Cursor/Grok requirement.
- In the editor, model-free skill discovery can precede any paid turn. Any paid editor acceptance must fit within the same six-turn budget.
- Preserve versions, requested/reported model identities, selected/active/pending states, request/run/workflow IDs, before/after Git checks, test results and redacted output in durable evidence. Retain failed attempts.

### Completion and scope boundaries

One Executor may work through all phases in a long session after approval. Record progress by completed artifact/test and resumable next step in Execution Notes; a context reset does not reset counts or require redrafting completed phases. Do not stop after writing a skeleton adapter or help text while ordinary in-scope implementation remains.

No autonomous Planner daemon, fleet/swarm, remote/cloud workers, ACP rewrite, universal third-party interoperability, global installation, publishing, merge, deployment, billing integration or quality/speed claims. No hot model replacement inside a running process. These exclusions do not waive automatic setup, Cursor Planner/Executor support or CLI model switching.

Create the named branch from the verified starting baseline at execution time; do not switch branch during this planning turn. Commit implementation coherently; no push/PR/merge. Preserve unrelated changes. Return READY FOR QA after implementation and the agreed checks, or explicitly report the remaining external acceptance blocker with completed work. The original Planner performs independent diff and behavior QA before APPROVED. Do not self-approve. A later correction budget follows the existing three-execution policy without resetting any predecessor.

### Source references and evidence boundaries

Verified 2026-09-24; recheck installed capabilities before depending on version-specific flags:
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters
- Cursor headless behavior: https://cursor.com/docs/cli/headless
- Cursor permissions: https://cursor.com/docs/cli/reference/permissions
- Cursor skills: https://cursor.com/docs/skills
- Installed Cursor help/version and failed authenticated model discovery described above.
- Existing local accepted behavior: docs/qa-a5-2026-09-24.md, docs/qa-a3-a4-correction-2026-09-24.md, docs/a2a-replacement-results.md, docs/a2a-coding-task-v1.md.

Public docs show the CLI mechanisms; they do not prove this account's model entitlement, installed permission enforcement or this repository's future integration.

---

## Execution Notes

Not started. Planning only. A5 has been archived with its approval and full execution/QA history. Complete plan is mirrored at docs/handoffs/A6-CLI-USABILITY-CURSOR.md; root HANDOFF.md is the active coordination surface. If the approved plan changes, synchronize Current Task in both before execution; execution notes/QA thereafter live in root HANDOFF.md.

---

## QA Feedback

Pending implementation and independent Planner QA.
