# A2A Executor implementation plan

Status: plan approved; A1 implemented / awaiting QA after correction of CLI exit codes and Claude result validation.
Date: 2026-09-14. Source baseline: `6201590bb98d7cc9514c04e7df14482695f8afed`.
Open PR check: `gh pr list --state open` returned no open PRs on 2026-09-14.

## Outcome

Keep the existing human → Planner → Executor → same Planner QA → human merge workflow. Add an optional A2A boundary so changing the configured coding Executor does not change the Planner's instructions or client/orchestration code. The existing direct-Claude execution path stays the default.

The Planner is the external chat agent. `drive` is a workflow instruction, not a coded Planner loop. Do not add a Planner service, a separate QA agent, agent routing, or parallel workers.

Add a small `handoff-cli` skill as the user-facing entry point for that Planner. It invokes the existing CLI and preserves the same responsibilities. The skill is useful with legacy execution immediately and with A2A after A3; it does not implement either transport.

The temporary experiment established that the same official-SDK A2A client could run identical coding requests and follow-ups through Claude and Codex. It did not establish production reliability or improvement in coding quality, speed, or cost. See [prototype evidence](a2a-prototype-results.md).

## How implementation and QA will run

1. Planner drafts one self-contained task in root `HANDOFF.md`; keep `DRAFT` until human approval. The current CLI reports its turn as UNKNOWN and refuses execution, which is intentional.
2. After approval, Planner sets `READY FOR EXECUTION` and invokes the existing handoff CLI. Use the current direct-Claude implementation to build all four tasks; do not switch the implementation workflow onto the new A2A path until A4 is verified and the human chooses it.
3. Executor implements and commits the named task. Planner reviews the actual branch/working-tree diff and executes its acceptance checks.
4. Request another execution only for a material acceptance failure. Stop after three unsuccessful QA rounds.
5. On QA approval, archive that handoff using `handoff archive`, update this tracker, and draft the next task. Each subsequent handoff is reviewed before execution. QA approval does not authorize merging, pushing, or publishing.

Use one implementation branch, `codex/a2a-executor-mvp`, with a recorded start/end commit per task. Review each task against its recorded start commit, including uncommitted changes, so later QA does not repeatedly revisit accepted earlier work. The final merge is the human's decision.

## QA agreement

Block on a reproducible failure in a supported workflow, materially incorrect code, a broken legacy path, failure to switch Executors through the common contract, or a practical safety regression such as duplicate execution, credential forwarding, wrong-workspace writes, or premature QA while work is still running.

Do not block on rare speculative edge cases, preferred internal abstractions, formatting, naming, or exact documentation wording. Documentation needs correct runnable commands, setup prerequisites, and honest limitations. Tests should check behavior, not prose. Note minor improvements once as optional; do not turn them into another execution round. Do not expand a task's acceptance criteria after implementation without an actual functional reason or explicit scope agreement.

Run the focused checks once after the final relevant change. Repeat or broaden only for a failure, changed code, or a concrete unresolved concern. Default automated tests use fake subprocesses and temporary repositories; paid model calls belong to explicitly identified live checks.

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

## Four implementation handoffs

| ID | Deliverable | Depends on | Status | QA evidence / commit |
|---|---|---|---|---|
| A1 | Standalone local A2A server, Claude adapter, provider-neutral client, request/result contract, and Planner-facing CLI skill | Existing baseline | implemented / awaiting QA | d615f09; correction b9bd0aa |
| A2 | Durable execution identity, reconnect/restart handling, and cancellation | A1 | Not started | — |
| A3 | Wire A2A into handoff CLI, workflow status, gates, and reporting | A2 | Not started | — |
| A4 | Codex adapter and real replacement validation through handoff CLI | A3 | Not started | — |

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

### A3 — Integrate the existing CLI and workflow

Update `bin/handoff` to select `legacy` (default) or `a2a` from local configuration, loading Python only for the latter. `execute` still blocks by default while polling; `resume` reconnects and `cancel` requests cancellation. `status` displays workflow status and execution status separately. `watch` consults outstanding execution state and never launches another run merely because Markdown still says READY FOR EXECUTION.

Update `skills/handoff-cli/SKILL.md` for the new approve/resume/cancel commands and A2A status semantics. Its execute/QA/drive workflow stays provider-neutral and always enters through `handoff`.

Use a short local submission mutex plus durable outstanding-run record to close the gap before an A2A task ID is known. Legacy and A2A execution both honor that record and the same workspace execution lock. The client records a pending submission before sending; the server owns the actual worker lock. Reconciliation clears the outstanding record only after terminal worker state and result capture. The client must not acquire a lock that the server must independently acquire for the same run.

The A2A request carries full HANDOFF.md content. The Claude adapter may continue editing local execution notes/status, but its early READY FOR QA cannot trigger QA until A2A terminal completion. Preserve Planner-owned task instructions and QA feedback, validating that an Executor did not alter them. A successful, valid terminal result is then reconciled to READY FOR QA. Failed, canceled, or unresolved tasks stop automatic dispatch and surface their reason.

Add `DRAFT` to the documented template flow: write the plan as DRAFT, obtain human approval, then activate execution. For A2A runs, `handoff approve` records the approved plan hash locally, sets READY FOR EXECUTION, and establishes the workflow ID. It is invoked by the human or Planner after explicit chat approval; this is an audit receipt, not cryptographic human authentication. Hash the branch and plan content excluding mutable Status, Execution Notes, and QA Feedback. Keep a separate hash of the complete submitted snapshot. Scope changes require renewed approval; QA feedback within approved scope does not.

Persist an A2A max execution/QA-round limit defaulting to three: one implementation plus at most two correction executions. Reconnects and duplicate transport requests do not count again. Failures before any worker starts do not consume an execution round. Do not retrofit a separate approval database into every historical legacy use case: document legacy's instruction-based limit and gate as legacy behavior, while moving the default template to DRAFT avoids the known watch-before-approval problem.

Configuration: locally excluded `.handoff-config.json`, `transport`, `a2a.agent_card_url`, `a2a.workspace_id`, credential file/reference, polling/request/execution timeouts, and `max_rounds`. Missing/invalid A2A settings fail clearly. Provider model/binary is server configuration; existing HANDOFF_MODEL stays meaningful only for legacy mode.

Keep `<run_id>-execute.log`, `-result.json`, and `-manifest.json`, adding immutable request snapshots and lifecycle events. New manifests carry workflow/execution/task IDs, endpoint identity, outcome, git snapshot, and neutral usage/cost; `runs` reads both new and old formats without counting a resumed run twice. A1 server run artifacts can be referenced/copied into this format; avoid two competing authoritative ledgers.

Fix the existing early lock release and recheck status after acquisition where the shared execution path is touched. Handle pre-existing dirty work explicitly: initial A2A execution requires a clean code baseline; continuation checks the last recorded post-run snapshot so the same workflow may retain uncommitted Executor changes. Never stash/reset user changes automatically.

Acceptance: current legacy smoke tests stay green without Python installed; actual `handoff execute` A2A fake-worker roundtrip; READY FOR QA only after terminal completion; watch does not duplicate an active/resumable task; DRAFT/unapproved/changed-scope plan refused; third failed QA round stops further execution; old and new run reporting works; client credentials are absent from subprocess output/env probes.

### A4 — Prove actual Executor replacement

Add a Codex adapter using its documented headless interface, configured server-side. Preserve the same coding profile, result schema, branch/workspace expectations, and Planner QA flow. Translate provider usage honestly. Native permission controls differ; review ordinary allowed edit/test operations and the no-publishing boundary for each adapter without claiming their sandbox semantics are identical.

Run the installed handoff CLI, not only the development helper, against two separately configured endpoints using identical disposable fixtures and the same complete HANDOFF plan. Run an implementation and Planner-requested correction for both. Compare the actual diff and tests. Also switch endpoints between an implementation and follow-up on one workflow to confirm the new Executor needs only the submitted handoff and git state, not the previous provider's session.

Acceptance: endpoint/configuration changes only; no provider conditionals in Planner/client workflow; both real providers pass implementation and correction QA; inspect Agent Cards and standard operations with an SDK client; record actual commands, model identities, time/usage, git evidence, and limitations. Default CI remains offline; live checks require working logins and an explicit command.

## Final completion bar

The feature is complete when the actual handoff CLI can switch Claude/Codex endpoints, run the existing Planner/QA loop against real git changes, and handle ordinary duplicate/disconnection/cancellation cases without losing workspace control. Human plan approval and final merge authority remain explicit. Keep A2A opt-in until these checks pass.

Not required for this MVP: independently authored third-party server, remote workspace transfer, distributed lock service, streaming/webhooks, agent routing, broad policy frameworks, exhaustive fault injection, or proof that A2A improves coding quality. State untested capabilities as untested rather than building them preemptively.
