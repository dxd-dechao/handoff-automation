# A2A Executor implementation plan

**A8-PREFLIGHT-SANDBOX (2026-09-25): executed; READY FOR QA.** Dispatch preflight, three-state liveness, sandboxed connect/skill/init errors, and section-boundary diagnostics. Evidence: [qa-a8-preflight-sandbox.md](qa-a8-preflight-sandbox.md).

**A7-SKILL-FIRST-SETUP (2026-09-25): executed; READY FOR QA.** Skill-first setup and use: `handoff skill install|status` (one installer, three hosts, safe upgrades), the Setup interview with A2A as the agreed default, a saved run mode (`init --mode`, `handoff mode`, status, watch enforcement, watcher record), `--json` output for the skill, and Executor isolation from the skill. On `codex/a7-skill-first-setup`. Evidence and limits: [qa-a7-skill-first-setup.md](qa-a7-skill-first-setup.md).

**A6-CLI-USABILITY-CURSOR (2026-09-24): executed; READY FOR QA.** Generated setup (`init`/`models`), managed service (`server`), Executor model selection within a workflow (`model`, `--after-current`), Cursor Executor adapter, and Cursor CLI/editor Planner integration, on `codex/a6-cli-setup-cursor-model-switch`. Evidence and limits: [qa-a6-cli-usability.md](qa-a6-cli-usability.md). The earlier status below remains the record for A1–A5.

**Planner QA, 2026-09-24, reviewed `1e09ec5`: APPROVED for the scoped local MVP.** A5 resolves the last material blocker (R3 startup cancellation). 99 Python tests and 17 legacy checks pass; the original independent cancel probe passes 4/4 runs across both fake adapters. R1/R2 and the retained live replacement evidence remain accepted. See the [final QA report](qa-a5-2026-09-24.md). Root HANDOFF.md is **A5-CANCEL-STARTUP, APPROVED**, after one execution; the predecessor retains its three executions and superseded history. No further implementation round or paid comparison is requested. Human merge/push/adoption remain separate. Full skill-host workflow and third-party interoperability remain disclosed limitations.

Continuation (2026-09-24, Executor): the A3-A4-COMPLETE handoff was executed. The A3 correction, Codex adapter, live replacement evidence, and skill host checks in Codex CLI are done; Cursor host discovery is untested. See product-status.md "Continuation results". The original Planner subsequently approved the scoped MVP after A5, as recorded above.

Status as of 2026-09-24 (Planner review, before the continuation): A1 and A2 QA approved; A3 implemented at `7c45000`, but not QA approved. Current regression suites pass (47 Python tests and 17 legacy checks); an independent probe reproduced a configuration-switch recovery/status defect. A4 is not started. Human retains manual Executor launch and merge/push decisions.
Created: 2026-09-14. Original source baseline: `6201590bb98d7cc9514c04e7df14482695f8afed`. Current inspected HEAD: `7c45000174ac583a2003f2b10ea2dbb5e0b6f7d7` on `codex/a2a-executor-mvp`.
Historical PR check: no open PRs on 2026-09-14; not rechecked for this status review.

See [current product status and remaining work](product-status.md) for the evidence, reproduced blocker, ordered delivery plan, and completion checklist. The sections below retain the original architecture and acceptance requirements.

Execution authorization (2026-09-24, before the continuation): the human requested one implementation agent to complete the remaining plan, with independent Planner QA afterward. Root `HANDOFF.md` was set to **A3-A4-COMPLETE**, READY FOR EXECUTION, for manual launch. That continuation has now finished and received the QA decision above. The old A3 task and findings remain in the local archive without claiming approval or resetting history. No agent has been launched by the Planner. Open PR check on this date returned none.

## Outcome

Keep the existing human → Planner → Executor → same Planner QA → human merge workflow. Add an optional A2A boundary so changing the configured coding Executor does not change the Planner's instructions or client/orchestration code. The existing direct-Claude execution path stays the default.

The Planner is the external chat agent. `drive` is a workflow instruction, not a coded Planner loop. Do not add a Planner service, a separate QA agent, agent routing, or parallel workers.

Add a small `handoff-cli` skill as the user-facing entry point for that Planner. It invokes the existing CLI and preserves the same responsibilities. The skill is useful with legacy execution immediately and with A2A after A3; it does not implement either transport.

The temporary experiment established that the same official-SDK A2A client could run identical coding requests and follow-ups through Claude and Codex. It did not establish production reliability or improvement in coding quality, speed, or cost. See [prototype evidence](a2a-prototype-results.md).

## How implementation and QA will run

1. Planner drafts one self-contained task in root `HANDOFF.md`; keep `DRAFT` until human approval. The current CLI reports its turn as UNKNOWN and refuses execution, which is intentional.
2. After approval, Planner activates the reviewed task as `READY FOR EXECUTION`; the human manually asks the Executor to run, as agreed in this project. Do not launch automatically. Use the current direct-Claude path for implementation; do not switch this checkout onto A2A until A4 is verified and the human chooses it.
3. Executor implements and commits the named task. Planner reviews the actual branch/working-tree diff and executes its acceptance checks.
4. Request another execution only for a material functional acceptance failure. After three unsuccessful executions, stop dispatching and have Planner review the scope, preserve passing work, and draft the smallest meaningful remaining task. Archive the exhausted task as superseded and link a constrained successor for human approval/manual launch. If only documentation remains, Planner fixes it directly and closes QA without another execution.
5. On QA approval, archive that handoff using `handoff archive`, update this tracker, and draft the next task. Each subsequent handoff is reviewed before execution. QA approval does not authorize merging, pushing, or publishing.

Use one implementation branch, `codex/a2a-executor-mvp`, with a recorded start/end commit per task. Review each task against its recorded start commit, including uncommitted changes, so later QA does not repeatedly revisit accepted earlier work. The final merge is the human's decision.

## QA agreement

Block on a reproducible failure in a supported workflow, materially incorrect code, a broken legacy path, failure to switch Executors through the common contract, or a practical safety regression such as duplicate execution, credential forwarding, wrong-workspace writes, or premature QA while work is still running.

Do not block on rare speculative edge cases, preferred internal abstractions, formatting, naming, or exact documentation wording. Documentation needs correct runnable commands, setup prerequisites, and honest limitations. Tests should check behavior, not prose. Note minor improvements once as optional; do not turn them into another execution round. Do not expand a task's acceptance criteria after implementation without an actual functional reason or explicit scope agreement.

Run the focused checks once after the final relevant change. Repeat or broaden only for a failure, changed code, or a concrete unresolved concern. Default automated tests use fake subprocesses and temporary repositories; paid model calls belong to explicitly identified live checks.

Planner is authorized to make documentation-only QA corrections directly at any round, then record the changes/checks and approve when functional acceptance passes. Runtime, tests, permissions, and behavior-changing skill/template edits remain implementation work. At the execution limit, reassess scope rather than repeating the broad task: identify the important blocker, narrow files/behavior/checks, preserve passing work, and keep deferred requirements visible. A successor does not mean the original broader goal has passed.

## Architecture decisions

| Concern | Decision |
|---|---|
| Protocol/runtime | A2A 1.0 JSON-RPC via the official Python SDK. Start with the tested `a2a-sdk==1.1.2`; lock dependencies. Python remains optional for the legacy path. |
| Endpoint selection | One explicitly configured Agent Card URL and credential reference. No discovery/routing and no automatic fallback to direct execution. |
| Scope | Same-machine registered checkout and task branch. No remote cloning, artifact application, worktree fleet, or automatic provisioning. |
| Instructions | Send the complete current HANDOFF.md snapshot plus execution metadata. Do not introduce a second user instruction/prompt channel. |
| Provider boundary | Binary, model, provider credentials, native flags, and output parsing live in server-side adapters. The A2A client never branches on provider identity. |
| Git authority | Planner inspects actual git changes and tests. An Executor result is a report; A2A COMPLETED is not QA APPROVED. |
| Long tasks | Submit non-blocking, save the task identity, poll. Reconnect to the original task. No streaming or webhooks in this MVP. |
| State | Server owns A2A execution state. Local client run record owns submission/reconciliation state. HANDOFF.md retains human workflow status. |
| Authentication | Local service bearer credential, separate from provider credentials. Adapter strips inherited provider auth variables before using the configured Executor login. Same OS user is not account/filesystem isolation. |
| Accounting | Neutral usage/cost fields with source/provenance; unknown values stay null. Preserve raw provider output and old Claude manifests. No inferred Codex prices or budget-enforcement claims. |

## Implementation handoffs and constrained follow-up

| ID | Deliverable | Depends on | Status | QA evidence / commit |
|---|---|---|---|---|
| A1 | Standalone local A2A server, Claude adapter, provider-neutral client, request/result contract, and Planner-facing CLI skill | Existing baseline | APPROVED | d615f09 + b9bd0aa; reviewed through 123eccc; 23 A2A + 17 legacy checks and five independent CLI probes passed |
| A2 | Durable execution identity, reconnect/restart handling, and cancellation | A1 | APPROVED; R3 closed by A5 | b8468ce + 756ad06; 35 A2A + 17 legacy checks and both independent cancellation/restart probes passed on 2026-09-15 |
| A3 | Wire A2A into handoff CLI, workflow status, gates, and reporting | A2 | APPROVED; recovery and R1 accepted | Reviewed through 8d99b93; 93 Python + 17 legacy checks; [current QA](qa-a3-a4-correction-2026-09-24.md) |
| A4 | Codex adapter and real replacement validation through handoff CLI | A3 | APPROVED for scoped local replacement experiment | [Live results](a2a-replacement-results.md); [current QA](qa-a3-a4-correction-2026-09-24.md) |
| A5 | Safe cancellation during worker startup | Preserved A1–A4 work | APPROVED at 1e09ec5, execution 1 | 99 Python + 17 legacy checks; independent cancel probe 4/4; [final QA](qa-a5-2026-09-24.md) |

### A1 — Make a real A2A execution path work

Add an optional Python package with a small `handoff-a2a` development command, official Agent Card/JSON-RPC routes, a neutral client, and Claude adapter. The server accepts only an approved-status HANDOFF snapshot in a registered git workspace, verifies the snapshot/branch/HEAD before starting, takes the existing per-workspace execute lock, runs the fixed ritual prompt, and returns status plus a structured result artifact. The neutral client has no arbitrary prompt option.

This stage is a runnable vertical slice, not just types/scaffolding. It does not change `bin/handoff` or the installed template yet. In-memory task state is explicitly acceptable at this stage; restart durability and cancel commands are A2. A dropped connection is reported as unresolved, never silently resubmitted or routed to legacy execution.

Expected files: `pyproject.toml`, `uv.lock`, optional runtime ignores, `src/handoff_a2a/{__init__,__main__,contracts,client,server,workspace}.py`, `src/handoff_a2a/adapters/{__init__,claude}.py`, `tests/test_a2a_*.py`, `docs/a2a-coding-task-v1.md`, and a short README addition. Adjacent small helpers inside the package are allowed when useful; no framework rewrite.

Also deliver the instruction-only skill at `skills/handoff-cli/SKILL.md`, with installation instructions in README. Keep one source in this repository; the user can install/link it into a supported skill directory for the Planner, including a user-level location when working across repositories. A1 does not modify global configuration or install the skill automatically.

The skill supports natural-language requests to use the handoff CLI and explicit selection, for example `$handoff-cli plan ...`, `$handoff-cli execute`, `$handoff-cli qa`, `$handoff-cli drive`, and `$handoff-cli status`. These are skill intents, not new CLI subcommands. Plan writes a DRAFT; execute dispatches the reviewed, approved plan; QA inspects actual git changes and relevant tests; drive follows the approved execution/QA loop; status uses existing read-only commands. Preserve approval already given in the conversation. Final merge remains a human gate. Use this plan's practical QA policy.

Resolve the target repository and installed `handoff` executable (or an explicitly configured checkout's `bin/handoff`), pass the repository explicitly, and inspect supported commands. Never assume the skill's installation folder is the target repo. Keep provider selection in the CLI. The skill must not activate Planner orchestration inside the headless Executor's fixed `execute the handoff` instruction or recursively launch another Executor. Do not invent `handoff plan`, `handoff qa`, or `handoff drive`; resume/cancel/approve are available only after A3 adds them.

Skill acceptance: valid discoverable metadata after installation into a temporary supported skill directory; a Planner-session status smoke against a disposable fixture; review plan/execute/QA/drive routing against current commands and approval/role boundaries. Record whether host discovery was actually tested or only instructions were reviewed. No paid coding calls or exact-prose tests are needed. [Official skill loading and invocation guidance](https://learn.chatgpt.com/docs/build-skills).

Acceptance: a fake Claude subprocess actually changes a temporary git fixture and HANDOFF notes/status through a real localhost A2A call; same client handles the initial and correction rounds as separate tasks; inspect the git changes independently; reject wrong workspace/snapshot/HEAD and concurrent execution; surface CLI/provider failures; retain valid zero/missing usage; existing 17-check smoke suite stays green. Automated tests never invoke paid models.

### A2 — Make ordinary interruptions recoverable

Add a single SQLite task/execution store. Persist request identity before spawning. The key is authenticated caller + execution ID; duplicate identical requests return the existing task, while changed content under that ID is rejected. Save the A2A task/context IDs and final artifacts. Dedupe and result retrieval survive restart.

Maintain one execution lock while the worker can write and through final result capture. On restart, reconcile recorded process identity; if it cannot establish that an old worker stopped, retain the workspace reservation and report recovery required. A simple conservative stop-and-recover path is sufficient; seamless continuation across service crashes is not required.

Add client resume/status/cancel and server deadlines. Connection timeout does not imply cancellation. Cancellation must stop and reap the owned worker and its ordinary child processes before releasing the workspace. Test this using a controlled child that launches a child process, not exotic process escape scenarios. If the host cannot stop the process tree, fail visibly and retain protection rather than claim cancellation succeeded.

Acceptance: reconnect without a second worker; lost-ack retransmission dedupes; repeated identical submission after restart does not spawn again; completed task survives restart; normal cancel and deadline expiry stop the controlled process tree; known active workspace is not reused prematurely. These are the practical asynchronous failure cases, not a distributed-systems stress suite.

A2 handoff decisions (2026-09-15): use one SQLite database for SDK Task snapshots and execution claims; deduplicate the full validated request by trusted caller identity + execution_id before dispatch. Save immutable client run records before submission and task IDs before polling. Add `handoff-a2a status/resume/cancel --run-record ... --credential-file ...`; the legacy `handoff` CLI and its skill stay unchanged until A3. Resume with a lost acknowledgement reuses the exact saved request/ID, only against an endpoint advertising this profile's durable deduplication support.

Own a POSIX worker process group and retain its workspace reservation through stop verification and evidence capture. Explicit cancel becomes CANCELED; server deadline becomes FAILED. Unknown ownership/stop state stays reserved and surfaces standard INPUT_REQUIRED plus recovery_required metadata. After restart, preserve completed results and conservatively stop/fail interrupted work when ownership is known; seamless model-session continuation is unnecessary. Add `store.py`, `processes.py`, and focused lifecycle tests, updating existing Python client/server/adapter components. Use controlled child writers and real server restarts to verify the behavior; no paid model calls or broad fault-injection suite.

### A3 — Integrate the existing CLI and workflow

Update `bin/handoff` to select `legacy` (default) or `a2a` from local configuration, loading Python only for the latter. `execute` still blocks by default while polling; `resume` reconnects and `cancel` requests cancellation. `status` displays workflow status and execution status separately. `watch` consults outstanding execution state and never launches another run merely because Markdown still says READY FOR EXECUTION.

Update `skills/handoff-cli/SKILL.md` for the new approve/resume/cancel commands and A2A status semantics. Its execute/QA/drive workflow stays provider-neutral and always enters through `handoff`.

Use a short local submission mutex plus durable outstanding-run record to close the gap before an A2A task ID is known. Legacy and A2A execution both honor that record and the same workspace execution lock. The client records a pending submission before sending; the server owns the actual worker lock. Reconciliation clears the outstanding record only after terminal worker state and result capture. The client must not acquire a lock that the server must independently acquire for the same run.

The A2A request carries full HANDOFF.md content. The Claude adapter may continue editing local execution notes/status, but its early READY FOR QA cannot trigger QA until A2A terminal completion. Preserve Planner-owned task instructions and QA feedback, validating that an Executor did not alter them. A successful, valid terminal result is then reconciled to READY FOR QA. Failed, canceled, or unresolved tasks stop automatic dispatch and surface their reason.

Add `DRAFT` to the documented template flow: write the plan as DRAFT, obtain human approval, then activate execution. For A2A runs, `handoff approve` records the approved plan hash locally, sets READY FOR EXECUTION, and establishes the workflow ID. It is invoked by the human or Planner after explicit chat approval; this is an audit receipt, not cryptographic human authentication. Hash the branch and plan content excluding mutable Status, Execution Notes, and QA Feedback. Keep a separate hash of the complete submitted snapshot. Scope changes require renewed approval; QA feedback within approved scope does not.

Persist an A2A max execution/QA-round limit defaulting to three: one implementation plus at most two correction executions. Reconnects and duplicate transport requests do not count again. Failures before any worker starts do not consume an execution round. Do not retrofit a separate approval database into every historical legacy use case: document legacy's instruction-based limit and gate as legacy behavior, while moving the default template to DRAFT avoids the known watch-before-approval problem.

When exhausted, route to external Planner scope review. Add `handoff archive <repo> --superseded` for an exhausted, fully reconciled workflow, preserving history and its unsuccessful disposition. A reviewed smaller successor starts as DRAFT, needs human approval, records parent_workflow_id, and may inherit the verified post-run baseline (including uncommitted work). It gets its own budget without resetting the predecessor's counters. Watch cannot make this transition or dispatch the draft. Documentation-only remaining work is handled by Planner directly, with no further Executor run.

Configuration: locally excluded `.handoff-config.json`, `transport`, `a2a.agent_card_url`, `a2a.workspace_id`, credential file/reference, polling/request/client-wait timeouts, and `max_rounds`. Server execution timeout remains server configuration. Missing/invalid A2A settings fail clearly. Provider model/binary is server configuration; existing HANDOFF_MODEL stays meaningful only for legacy mode.

Keep `<run_id>-execute.log`, `-result.json`, and `-manifest.json`, adding immutable request snapshots and lifecycle events. New manifests carry workflow/execution/task IDs, endpoint identity, outcome, git snapshot, and neutral usage/cost; `runs` reads both new and old formats without counting a resumed run twice. A1 server run artifacts can be referenced/copied into this format; avoid two competing authoritative ledgers.

Fix the existing early lock release and recheck status after acquisition where the shared execution path is touched. Handle pre-existing dirty work explicitly: initial A2A execution requires a clean code baseline; continuation checks the last recorded post-run snapshot so the same workflow may retain uncommitted Executor changes. Never stash/reset user changes automatically.

Acceptance: current legacy smoke tests stay green without Python installed; actual `handoff execute` A2A fake-worker roundtrip; READY FOR QA only after terminal completion; watch does not duplicate an active/resumable task; DRAFT/unapproved/changed-scope plan refused; third failed QA round stops further execution; old and new run reporting works; client credentials are absent from subprocess output/env probes.

A3 handoff decisions (2026-09-15): keep Bash as the public CLI and use small Python integration/workflow/config/reporting helpers for A2A. Reuse A2 run records and server SQLite; an outstanding pointer and short cross-transport submission mutex prevent duplicate dispatch. Approve records the plan and workflow without resetting used rounds on reapproval. Unknown submissions reserve a round until reconciled; failed delivery sets an automatic-dispatch hold that watch cannot clear. Archive closes approved work or explicitly supersedes exhausted work after Planner scope review before a linked constrained draft.

Capture code content fingerprints (including nonignored untracked files), check continuation against the last post-run snapshot, and validate the submitted expected fingerprint again under the server worker lock. Advertise the added profile capability. Publish enough authenticated worker-start/stop/partial-evidence data for correct counting and reconciliation. Keep the server manually started locally, legacy/mixed reporting usable without Python, and the skill provider-neutral. Test through actual bin/handoff subprocesses; automatic daemon management and Herdr integration are outside A3.

### A4 — Prove actual Executor replacement

Add a Codex adapter using its documented headless interface, configured server-side. Preserve the same coding profile, result schema, branch/workspace expectations, and Planner QA flow. Translate provider usage honestly. Native permission controls differ; review ordinary allowed edit/test operations and the no-publishing boundary for each adapter without claiming their sandbox semantics are identical.

Run the installed handoff CLI, not only the development helper, against two separately configured endpoints using identical disposable fixtures and the same complete HANDOFF plan. Run an implementation and Planner-requested correction for both. Compare the actual diff and tests. Also switch endpoints between an implementation and follow-up on one workflow to confirm the new Executor needs only the submitted handoff and git state, not the previous provider's session.

Acceptance: endpoint/configuration changes only; no provider conditionals in Planner/client workflow; both real providers pass implementation and correction QA; inspect Agent Cards and standard operations with an SDK client; record actual commands, model identities, time/usage, git evidence, and limitations. Default CI remains offline; live checks require working logins and an explicit command.

### A9 — Fingerprint rebaseline

Untracked Python bytecode (`__pycache__`, `.pyc`, `.pyo`) is not code: it stays out of `code_fingerprint` and `dirty_code_paths` even when the target repository does not gitignore it. Tracked bytecode still counts. A fingerprint recorded before that filter can still mismatch; re-approving a *changed* plan for an existing workflow, when a post-run fingerprint is already set and no round is reserved, records the current clean workspace as the new baseline (`previous_post_run_fingerprint`, `rebaselined_at`). A dirty tree is refused and nothing is written. First approval and `archive --superseded` inheritance are unchanged. There is no workflow history log; the receipt fields are the record.

### A10 — Slim handoff template

The installed `templates/HANDOFF.md` preamble stays at or under 3 KB: opt-in banner, roles, Executor rules, and Status values. Planner loop rules live in the skill. `handoff template refresh` replaces only the preamble of an existing file and leaves `## Current Task` onward byte-for-byte. A sandboxed status probe of an outstanding run is `execution: UNKNOWN` with `probe: not_permitted`, distinct from `UNRESOLVED`. Known follow-ups are listed at the end of `docs/product-status.md` and are not fixed here.

### A11 — Handoff integrity

A HANDOFF file is well-formed only with exactly one `## Current Task`, one `## Execution Notes`, and one `## QA Feedback`, in that order, each as a whole line. A bad delivery is rejected and the bytes the server evaluated are kept. On A2A, `handoff qa` writes QA Feedback and Status. `archive` refuses a malformed file or a plan that changed since approval. Legacy archive checks the same headings and stays Python-free; `handoff qa` is A2A only.

### A12 — Polish and sandbox tests

`init` compares preambles in memory, so a read-only temp directory does not fail it. `template refresh` keeps the previous file mode and the file's own line ending, and accepts a CRLF `## Current Task`. `handoff qa` leaves every byte outside the QA body and the Status value unchanged, and refuses when the plan already differs from the approval receipt. Re-approval records a new baseline only when the workspace fingerprint changed. `execute` and `resume` take `--wait` (a positive number up to 1800) for that call only. Tests that need process probes, localhost, or a started service are marked `server`.

## Final completion bar

The feature is complete when the actual handoff CLI can switch Claude/Codex endpoints, run the existing Planner/QA loop against real git changes, and handle ordinary duplicate/disconnection/cancellation cases without losing workspace control. Human plan approval and final merge authority remain explicit. Keep A2A opt-in until these checks pass.

Not required for this MVP: independently authored third-party server, remote workspace transfer, distributed lock service, streaming/webhooks, agent routing, broad policy frameworks, exhaustive fault injection, or proof that A2A improves coding quality. State untested capabilities as untested rather than building them preemptively.
