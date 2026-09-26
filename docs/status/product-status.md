# Product status and path to the A2A goal

Reviewed: **2026-09-24**. Checkout: `codex/a2a-executor-mvp`, commit `1e09ec58f4f059c9d7f3e01fe875eb01b99ce588`.

**A14 update (2026-09-26, Executor self-report; independent Planner QA pending).** An unreadable Planner skill folder is reported as `unknown`. `handoff status`, `handoff skill status`, and `init` keep working and name it. Install into that folder is refused and writes nothing. See [the A14 report](../qa/qa-a14-unreadable-skill-roots.md).

**A13 update (2026-09-26, Executor self-report; independent Planner QA pending).** `handoff qa --append` from READY FOR QA appends the new round and sets Status in one write. Stale Planner skill copies (`outdated`, or `elsewhere` with different content) are reported by `skill status`, `init`, and `status`, and are not modified. See [the A13 report](../qa/qa-a13-qa-append-stale-skill.md).

**A12 update (2026-09-25, Executor self-report; independent Planner QA pending).** Init compares preambles without a temp file, refresh keeps the file mode and each file's line endings, `handoff qa` edits only the QA body and Status and refuses a plan that already changed, rebaseline is recorded only when the snapshot changes, and `execute`/`resume` take `--wait`. The `server` pytest marker is the suite that needs process probes (including a child `handoff` probe), localhost, a started service, or a pseudo-terminal. See [the A12 report](../qa/qa-a12-polish-sandbox-tests.md).

**A11 update (2026-09-25, Executor self-report; independent Planner QA pending).** A delivery with a duplicated, missing, or reordered `## Current Task`, `## Execution Notes`, or `## QA Feedback` heading is rejected. `handoff qa` is the A2A way to write QA Feedback. `archive` refuses a damaged or changed plan. Each run saves the HANDOFF bytes the server evaluated. See [the A11 report](../qa/qa-a11-handoff-integrity.md).

**A10 update (2026-09-25, Executor self-report; independent Planner QA pending).** The HANDOFF preamble is a short Executor contract. `handoff template refresh` replaces only that preamble. A sandboxed status probe of an outstanding run is `UNKNOWN`, not `UNRESOLVED`. See [the A10 report](../qa/qa-a10-slim-handoff-template.md).

**A9 update (2026-09-25, Executor self-report; independent Planner QA pending).** Untracked Python bytecode is left out of the code fingerprint and the dirty-path list. Re-approving a changed plan records the current workspace as the new baseline when one already exists. A baseline recorded before this change that included bytecode still mismatches; revise Current Task and re-approve to recover. See [the A9 report](../qa/qa-a9-fingerprint-rebaseline.md).

**A8 update (2026-09-25, Executor self-report; independent Planner QA pending).** `handoff preflight` lists every execute blocker and its fix. A sandboxed probe is `unknown`, not stopped. `init --planner` keeps setup when the skill install fails. See [the A8 report](../qa/qa-a8-preflight-sandbox.md).

**A7 update (2026-09-25, Executor self-report; independent Planner QA pending).** Branch `codex/a7-skill-first-setup` from `2a5bf25`. The `handoff-cli` Planner skill is now the default entry point. After `handoff skill install` (Cursor, Codex, or Claude Code; project or explicit `--user`), the human asks the Planner to "set up handoff". The skill checks existing state with `--json`, proposes managed A2A for explicit agreement, asks provider, model, Codex effort, and run mode, then runs `init` and `server start`. The run mode (drive or watch) is saved by the CLI (`handoff mode`), shown in status, and enforced by watch. A Cursor CLI Planner passed the bounded live check after one failed turn led to a skill fix. Cursor Editor discovery remains a manual pending step. See [the A7 report](../qa/qa-a7-skill-first-setup.md).

**A6 update (2026-09-24, Executor self-report; independent Planner QA pending).** Branch `codex/a6-cli-setup-cursor-model-switch` from `7ee6d78`. The three user requirements are implemented through public commands:

1. `handoff init` generates every JSON, token, exclude, and optional Cursor skill file (guided on a TTY or scripted by flags), and preserves them on re-init. `handoff server start|status|stop` runs a verified local service.
2. Cursor works as Executor (`adapters/cursor.py`) and as Planner (project-local skill; `handoff planner` launches the Cursor CLI).
3. `handoff model` switches the Executor between executions, or with `--after-current` during one, without JSON edits or restarting the server or watch.

Live, one approved workflow used a Cursor CLI Planner draft, Cursor Grok and Composer executions, a Codex continuation, and Planner QA. The Codex continuation's delivery failed because it edited the QA section; it was correctly refused. Cursor editor skill discovery is still a manual pending step. See [the A6 report](../qa/qa-a6-cli-usability.md) and [setup guide](../guides/cli-setup-and-models.md). The sections below predate A6.

**The scoped local A2A MVP passes Planner QA.** R1/R2 were accepted previously; A5 now resolves R3 startup cancellation. **99 Python tests and 17 legacy checks pass.** The original independent cancellation probe passes 4/4 runs across both fake providers, with no surviving worker or delayed write after cancellation. Deterministic regressions cover cancellation before/during launch, child cleanup, and safe workspace reuse. See the [final QA report](../qa/qa-a5-2026-09-24.md).

Root `HANDOFF.md` is **A5-CANCEL-STARTUP, APPROVED**, after its first execution. The three-execution predecessor remains archived as **superseded, not approved**, preserving its history. No further implementation correction or paid comparison is required for this scope. Human merge/push and target-repository adoption remain pending; legacy remains the default.

The demonstrated benefit is Executor replacement by endpoint configuration while retaining the Planner workflow and git-based QA. This adds local server/credential/Python setup; it does not establish faster, cheaper, or better coding. Full host-driven skill planning/approval/QA, Cursor discovery, and third-party-server interoperability remain untested, disclosed limitations. The historical sections below preserve earlier assessments; use this decision for current status. Planner changed documentation only.

**Executor update (2026-09-24, A3-A4-COMPLETE continuation; independent Planner QA pending).** Implemented and self-tested:
- the A3 configuration-switch correction;
- a related failed-delivery routing fix;
- a production Codex adapter behind the same server boundary.

Live-verified: Claude, Codex, and a mid-workflow Claude→Codex switch through the real CLI, with the defects found live fixed ([results](../qa/a2a-replacement-results.md)). Skill discovery and invocation were checked in the Codex CLI host; the Cursor host is untested. Details are in [Continuation results](#continuation-results-a3-a4-complete-executor-self-report) below. Nothing here is QA approval.

Execution decision (2026-09-24, before the continuation): the human requested one agent to finish the remaining plan and return to the original Planner for independent QA. Root `HANDOFF.md` was set to **A3-A4-COMPLETE**, READY FOR EXECUTION, for manual launch. That continuation and its R1/R2 correction are complete; the cancellation follow-up is now accepted in A5. The original A3 task/findings remain in the local archive. A3/A4 and the A5 lifecycle follow-up now pass Planner QA; the adoption/merge decision remains pending.

Current testing status and known limits: [testing-status.md](testing-status.md).

## Goal and scope

Keep **Human → Planner → Executor → same Planner QA → correction → human merge**. The experiment succeeds if the Planner can select a different coding Executor through endpoint configuration while keeping its orchestration, coding contract, and git-based QA unchanged.

A2A standardizes transport, task lifecycle, and artifacts. Our `urn:handoff-automation:coding-task:v1` profile adds coding-specific workspace, plan, identity, and result requirements. Compatibility therefore means **A2A plus this coding profile and its required capabilities**; an arbitrary A2A agent is not automatically a compatible coding Executor.

The MVP is local: a manually started loopback server operates on a registered checkout. SQLite stores task state on disk. No cloud hosting or separately hosted database is required. Remote workspace transfer, swarms, routing, parallel workers, and a coded Planner service remain outside scope.

## What exists in this checkout

| Area | Verified implementation | Evidence boundary |
|---|---|---|
| Planner and QA | External chat agent follows `HANDOFF.md`; `drive` is an instruction, not a CLI subcommand or autonomous service | Planner judgment and human approval remain external responsibilities |
| Legacy execution | Bash `bin/handoff` directly launches headless Claude; default when config is absent | 17 legacy smoke checks pass; Python-free execution also has a Python-suite fixture |
| A2A boundary (A1) | Official SDK, Agent Card, JSON-RPC, complete handoff snapshot, neutral client, Claude adapter | Previously QA approved; Claude and Codex adapters now exist |
| Durable lifecycle (A2) | SQLite task/claim store, execution-ID deduplication, saved client records, polling, restart reconciliation, process-group cancellation/deadlines | Historical approval retained; R3 closed by A5 with deterministic and independent cancellation checks |
| CLI integration (A3) | Optional `.handoff-config.json`; approve/execute/status/resume/cancel/watch/archive routing; outstanding runs stay authoritative across config changes; failed/canceled deliveries restore the submitted Status | Saved-endpoint recovery and R1 dispatch-hold correction accepted; scoped MVP QA approved |
| Approval and scope | Local plan-hash receipt, default three-execution budget, reservations, superseded history and successor links | Receipts are audit records, not proof of a human's identity; legacy limits remain instruction-based |
| Workspace protection | Submission mutex, outstanding-run record, worker lock, branch/HEAD checks, code fingerprints and continuation baselines | Transport-switch recovery accepted; A5 prevents premature lock release during worker startup |
| Auth and accounting | Service bearer; Claude auth-variable stripping; neutral usage/cost fields and provenance; manifests and events | Same OS user is not filesystem/account isolation; missing cost remains unknown |
| Skill entry point | Repository-owned `skills/handoff-cli/SKILL.md` routes Planner intents through the CLI | Codex CLI host: discovery from a temporary repo location and `$handoff-cli status` invocation verified. Cursor host untested |
| Executor replacement (A4) | `adapters/{base,claude,codex}.py`; server config selects exactly one adapter; Agent Card names the Executor | Fake Claude/Codex share the profile tests; live Claude, Codex, and mixed workflows pass fixture QA ([results](../qa/a2a-replacement-results.md)); fixture evidence and R2 accepted; scoped MVP QA approved |

Implementation map: `bin/handoff`; `src/handoff_a2a/{client,contracts,server,store,processes,workspace}.py`; A3 adds `{integration,workflow,config,reporting}.py`. The continuation moved all provider-specific construction, environment, and parsing into `adapters/{claude,codex}.py` behind `adapters/base.py`. `server.py` only selects the configured adapter. The explicit live check is `scripts/a2a_live_check.py`.

## Historical verification before the continuation (2026-09-24)

| Check | Observed result |
|---|---|
| `./bin/handoff status .` | `READY FOR QA`, turn `PLANNER`, current task A3 |
| `bash -n bin/handoff` | Passed |
| `bash tests/smoke-manifest.sh` | 17 passed, 0 failed |
| `UV_CACHE_DIR=/private/tmp/handoff-qa-uv-cache uv run --offline --extra test python -m pytest -q` | 47 passed; 100 SDK/protobuf deprecation warnings; no model calls |
| Independent fake-worker configuration-switch probe | Failed expected status/recovery behavior; details below |

The first sandboxed Python run could not bind localhost or perform the process checks (18 passed, 29 failed). The same suite passed with localhost/process permissions. Those sandbox failures are not counted as product failures. Existing tests cover real local HTTP calls and controlled subprocesses; passing them does not establish real-provider replacement or complete A3 acceptance.

### Reproduced A3 blocker: changing transport hides an outstanding run (fixed in continuation; QA pending)

Using a disposable git repository and the existing fake-worker harness:

1. Configure A2A, approve a DRAFT, and execute a worker that writes `READY FOR QA` early but continues running. Set client wait to one second and worker sleep to 25 seconds.
2. Execution returns unresolved (`2`). `handoff status` correctly shows Markdown `READY FOR QA`, execution `WORKING`, and turn `WAIT`.
3. Change `.handoff-config.json` to `{"transport":"legacy"}` while the original worker is active.
4. `handoff status` now reports turn `PLANNER`, with no outstanding execution shown. Both `handoff resume` and `handoff cancel` exit `1`, saying they require A2A configuration. The worker lock is still present.
5. Restore the original config and cancel: the original run becomes `canceled` and its worker lock is removed. No worker was left running by the probe.

This violates A3's existing requirement that outstanding work remain visible and recoverable using its saved endpoint/request/credential reference after configuration changes. It can send the Planner to QA while code is still changing. It is not a documentation preference or a speculative edge case.

Relevant code: Bash `cmd_status`, `cmd_resume`, `cmd_cancel`, and transport routing; Python `cmd_resume_async` / `cmd_cancel_async` currently require live A2A config before loading the saved run. Existing `test_duplicate_dispatch_and_legacy_switch` checks refusal of another execution, then restores config; it does not exercise status/resume/cancel during the switch.

Local supplementary evidence: `/private/tmp/handoff-status-20260924/probe_config_switch.py` and `config-switch-result.json`. These temporary files are not dependencies; the reproduction and observed results above are the durable record. The correction should add a repository regression test.

Continuation: `5197e2f` adds `test_config_switch_to_legacy_keeps_outstanding_run_authoritative` and `test_config_removed_cancel_uses_saved_run_and_releases_once` (both providers). On `7c45000` both fail with `'PLANNER' == 'WAIT'`; they now pass.

## Continuation results (A3-A4-COMPLETE, Executor self-report)

Commits on `codex/a2a-executor-mvp` after `7c45000`: `5197e2f` (A3 fix), `b57729a` (Codex adapter), `e582fcb` (live check), `93c5d13` / `e595bb6` / `6c488e4` (fixes from live evidence), then the documentation commit. Independent Planner QA is still pending for A3 (from `756ad06`) and for this continuation (from `7c45000`).

| Area | State | Evidence |
|---|---|---|
| A3 config-switch recovery | Implemented, self-tested | CLI tests: switch to legacy, switch to another endpoint, remove config; saved-run resume/cancel; missing saved credential stays unresolved; one manifest; one round; no premature lock release |
| A3 failed-delivery routing (found in continuation) | Implemented, self-tested, live-observed | A worker-written `READY FOR QA`/`APPROVED` from a FAILED/CANCELED run no longer reaches "Planner QA"/"human merge". The Status is restored; `last:` and Planner review are shown. Tests cover provider error, executor-written APPROVED, mangled plan, and cancel |
| A3 watch after timeout/failure/cancel | Self-tested | Public `handoff watch`: reconciles a timed-out run once and notifies once; does not retry a held failure or re-dispatch after cancel |
| A3 constrained successor | Self-tested | Exhausted workflow → `archive --superseded` → approved successor inherits the post-run fingerprint, including uncommitted work, and executes; parent counters stay 3/3 |
| Codex adapter | Implemented, self-tested, live-verified | Same roundtrip/CLI/failure/usage/cancel/deadline tests for fake Claude and fake Codex; live results report |
| Real replacement through CLI | Live-verified (Executor-run fixture QA) | Claude, Codex, and Claude→Codex mixed workflows pass implementation and correction on identical fixtures ([results](../qa/a2a-replacement-results.md)) |
| Skill: metadata, host discovery, invocation | Codex CLI host verified | `codex debug prompt-input` lists `handoff-cli` from `<fixture>/.agents/skills` (control repo: absent; no user-level install). One read-only `codex exec '$handoff-cli status'` session chose the skill, ran `handoff status "<repo>"`, and launched no worker |
| Skill: plan/approve/manual-launch/QA routing in a host | Partly untested | The CLI routing each intent uses (approve, execute, status `next:`, archive) was exercised by the live workflows. A host-driven plan→approve→QA conversation was not run (it needs an interactive Planner session) |
| Cursor host discovery | **Blocked/untested** | Cursor cannot be driven from this session. Follow-up: copy `skills/handoff-cli` to `<disposable repo>/.cursor/skills/handoff-cli`, open it in Cursor, ask "handoff status", and confirm the skill is selected and runs `handoff status "<repo>"` |

Portability benefit observed: replacing Claude with Codex required a second locally started server and a one-line `agent_card_url` change. The Planner steps, client, approval receipt, round budget, and git-based QA did not change. A workflow continued on a different provider mid-way.

Setup burden observed: Python/uv, a manually started server per provider, a service credential file, per-provider Executor logins, and provider-specific boundary work. Codex needed operator-skill isolation, native loading of the git-excluded HANDOFF.md, and a sandbox profile that allows `.git`. Neither live defect was visible in fake-worker tests.

No claim is made about coding quality, speed, cost, remote/third-party compatibility, or production readiness.

## Original delivery plan (historical; implementation QA is complete)

### 1. Correct the A3 recovery gap, then finish A3 QA

**Proposed narrow Executor scope:** `bin/handoff`, `src/handoff_a2a/integration.py`, relevant saved-record/config helpers only if needed, and `tests/test_a2a_cli.py`. Reuse A2's task ownership and saved records; do not rewrite the server lifecycle.

When an outstanding A2A run exists, status/recovery must resolve that run before consulting the current transport selection. Resume/cancel must use its saved endpoint and credential reference, with sensible wait defaults if current config is absent. Missing saved credentials or an unavailable endpoint should remain a visible unresolved condition. New execution must stay blocked until reconciliation; switching config must neither reroute the saved run nor bypass its round accounting.

Acceptance: exercise the actual CLI with a live fake worker; switch to legacy and remove config in separate cases; status stays WAIT/RECOVERY while appropriate; saved-run resume/cancel reaches the original endpoint; no duplicate worker or premature QA; one manifest and one consumed round for that execution. Normal legacy use without outstanding A2A work stays Python-free.

Then Planner completes the existing A3 acceptance review against baseline `756ad06`, retaining passing work. In particular, verify public-CLI watch behavior after timeout/failure/cancel, safe terminal reconciliation, and an actual execution of the approved constrained successor inheriting uncommitted work. Current tests verify pieces of these flows, but do not complete all of those public-CLI scenarios. Add tests only for meaningful gaps; do not broaden this into exhaustive fault injection.

Exit: concrete material findings resolved, focused checks plus the existing suites pass, and Planner records A3 APPROVED. Current status review does not grant that approval.

### 2. A4a — Add a Codex Executor behind the same boundary

**Proposed modules:** add `src/handoff_a2a/adapters/codex.py`; introduce only the small shared adapter interface/outcome type needed in `adapters/`; update `server.py` configuration, adapter construction, Agent Card identity, environment handling, and output interpretation. Keep current Claude server configuration compatible. Add adapter/config tests and parameterize applicable roundtrip/lifecycle fixtures.

Server owns process start/stop and locks for both providers. Each adapter supplies its native command, isolated child environment, and output-to-neutral-result conversion. Inspect the installed Codex headless interface when drafting this task; do not hardcode unverified flags or assume Claude permission settings govern Codex. Verify allowed edit/test operations and the human-only publishing boundary for each provider.

Exit: fake Claude and fake Codex both pass the same coding profile, success/failure, usage, and cancellation checks. The Planner skill, neutral client, approval/round logic, and result contract contain no provider-specific branch. Unknown Codex cost remains null. No SDK upgrade, remote workspace support, or extra orchestration is necessary.

These A4 subdivisions are work phases inside the authorized A3-A4-COMPLETE continuation; they are not separate dispatches. The human still launches that continuation manually.

### 3. A4b — Prove replacement through the real CLI

Create a repeatable disposable-repository fixture and an explicitly invoked live-check command/report. Keep paid calls out of default tests. Human manually initiates the approved experiment with working provider logins; use small named-file tasks and the existing execution limits.

| Scenario | Required evidence |
|---|---|
| A2A → Claude implementation and correction | Actual `handoff approve/execute`, git diff/tests, same workflow ID, new execution/task ID for correction |
| A2A → Codex, same fixture and plan | Same commands and QA criteria; only endpoint/server configuration differs |
| Claude implementation → Codex correction within one workflow | New Executor continues from handoff plus git state without the old provider's session; approval, rounds, and audit history preserved |
| Ordinary lifecycle behavior with both adapters | Controlled tests confirm no double execution after resume and no release before worker stop; live results are inspected independently |

Start each independent comparison from the same clean commit. Switch providers only after the preceding run is reconciled. Record CLI/provider versions and model identities, Agent Cards/profile capabilities, redacted commands, run/workflow/task IDs, commits/diffs, acceptance results, elapsed time, usage/cost provenance, and any manual recovery. Publish a repository evidence report, not only temporary file paths.

Exit: both real providers pass implementation and correction QA, and the mixed-provider workflow passes with no changes to Planner/client orchestration. Generic SDK checks verify the advertised operations/profile. This demonstrates replacement between two adapters behind our server; independently authored third-party interoperability remains untested.

### 4. Validate the skill entry point and decide whether to adopt

After implementation acceptance, test discovery/invocation in the chosen Planner host using a temporary supported skill location, or install in the user's chosen location when requested. Demonstrate status, drafting, approval/manual-launch boundaries, and QA routing against a disposable repo. Source metadata tests alone are not proof that the host loads the skill. No global installation is implied by this plan.

Planner records the replacement findings and recommends whether A2A's portability is useful enough to justify Python, a local server, credentials, and durable state. The prior prototype is feasibility evidence only; neither it nor this review proves faster, cheaper, or better coding. If a speed/cost claim matters, run a matched direct-Claude comparison and describe the small-sample limits.

The human retains final merge/push and target-repository opt-in decisions. Legacy remains the default. A functioning MVP does not require automatic server startup, cloud hosting, or a general-purpose agent platform.

## Completion checklist

- [x] A1: local A2A execution slice, previously QA approved.
- [x] A2 lifecycle acceptance restored by A5; historical A2 approval is retained.
- [x] A3 saved-endpoint recovery and R1 dispatch hold accepted.
- [x] Production Codex adapter and R2 full handoff delivery accepted; provider logic remains behind the Executor boundary.
- [x] Claude/Codex and mid-workflow replacement fixtures independently checked against git and manifests.
- [x] Durable replacement evidence distinguishes observed benefit from untested claims.
- [x] A5 focused cancellation correction independently QA approved at 1e09ec5.
- [x] Overall scoped local MVP passes Planner QA; R1/R2/R3 closed.
- [ ] Full Planner skill workflow in the selected host. Codex discovery/status is Executor-reported verified; full conversation and Cursor remain untested, a disclosed nonblocking limitation.
- [ ] Human decides merge/push and whether to enable A2A in a target repo.

Execution policy throughout: human approval followed by manual Executor launch; Planner owns QA. After three executions, preserve passing work and narrow the remaining material scope into an explicitly linked successor. Planner handles documentation-only corrections directly. Do not reset predecessor counters, relax the original goal silently, or launch another broad round merely to polish prose.

## Known follow-ups

None open.
