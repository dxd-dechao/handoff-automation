# Independent QA — third execution and constrained follow-up

**Follow-up:** R3 was resolved and A5 independently approved at `1e09ec5`; the scoped local MVP now passes Planner QA. See [final QA](qa-a5-2026-09-24.md). This report preserves the earlier finding and three-execution scope decision.

Reviewed on 2026-09-24 at **8d99b9334d529960781f749017d9258192b0c7c2**, correction baseline `e0bbc23`.

**R1 and R2 are accepted. Overall MVP approval is withheld for one confirmed cancellation defect.** This is the third recorded product execution; the broad A3-A4-COMPLETE task must not enter a fourth retry. Preserve its work/history and use the constrained **A5-CANCEL-STARTUP** draft for human approval and manual launch.

## Closed findings

- **R1, watch hold across transport switches:** accepted at `90896f2`. Reran the original independent fake-worker probe: after A2A cancellation with configuration switched to legacy, the saved hold remained true, rounds remained 1, and `legacy_launched_without_review` changed from true to **false**. The new public-CLI regression matrix covers config removal/switch, canceled/failed deliveries, successful completion notification, and explicit reviewed execution.
- **R2, full Codex handoff with repository instructions:** accepted at `246981c`. The server now passes the validated snapshot to the adapter; Codex receives it through developer_instructions, alongside its repository guidance. The ritual prompt stays fixed. Reran native prompt inspection: the handoff is present both with and without AGENTS.md. Repository tests additionally cover AGENTS.override.md, complete content, no duplication, and TOML escaping. No paid model call was necessary.
- `8d99b93` documents the behavior. The minor legacy signal-trap change exits on INT/TERM instead of continuing with its lock released; this does not establish broader legacy process-group guarantees.

The prior retained Claude, Codex, and mixed-provider fixture outcomes remain valid small-fixture replacement evidence. No new paid experiment or broad implementation is requested.

## R3 — P1: immediate cancellation releases protection before a worker stops

The Executor reported this as pre-existing. Planner independently reproduced it with unmodified production code, ordinary localhost A2A requests, and the existing fake workers:

1. Create a normal fixture with `app.py` containing `value = 0`; set the fake worker's sleep to three seconds.
2. Submit a coding task and immediately call CancelTask, without waiting for WORKER_STARTED.
3. The response is TASK_STATE_CANCELED, execute.lock is absent, and app.py still contains value = 0. A live worker PID is observable after this response.
4. Wait past the scheduled write. The worker changes app.py to value = 1 while the task remains TASK_STATE_CANCELED and the lock remains absent.

| Provider | Independent repetitions | CANCELED and lock absent | Wrote code afterward |
|---|---:|---|---|
| Fake Claude | 2 | Both | Both |
| Fake Codex | 2 | Both | Both |

All four cases reproduced the safety failure. The probes used no injected runtime delay or paid model. All probe workers exited or were cleaned up. This is a supported early-cancellation failure, not a speculative process escape scenario. Releasing the workspace while a canceled worker can still write permits overlap with later work and makes the cancellation result materially false.

Relevant code at the reviewed commit: `src/handoff_a2a/server.py:779` awaits `asyncio.to_thread(start_owned, ...)` before assigning runtime.owned; `ExecutionRuntime.stop_worker` at line 347 treats owned=None as already stopped. `finalize` can therefore publish cancellation and release the lock without owning an in-flight spawn. **Diagnosis hypothesis:** cancellation interrupts the await while the synchronous startup thread continues and its returned handle is lost. The successor must confirm and correct the ordering rather than blindly apply a particular patch.

The existing `test_cancel_stops_worker_and_marks_canceled` checks code only immediately after cancellation. Its passing state/lock assertions miss delayed writes. The observed green regression suite is therefore not sufficient evidence of this specific guarantee. Historical A2 approval remains in the record, but this newly verified defect blocks current MVP acceptance.

## Verification record

- Bash syntax: passed.
- Legacy smoke: **17 passed**.
- Python regression suite: **93 passed**, 100 SDK/protobuf deprecation warnings, 236.07 seconds.
- Both CLI help commands and `git diff --check`: passed before final QA documentation edits.
- Original R1 independent probe: passed (`watch-fh2w0mdj/result.json`).
- Original R2 native prompt inspection: passed (`prompt-ttu8cj77/result.json`).
- Additional early-cancel probe: **4/4 reproduced R3** (`early-cancel-_hsquly3/results.json`).

Supplementary scripts/results live under `/private/tmp/handoff-final-qa-20260924/`; the durable reproduction is recorded above. `probe_early_cancel.py` makes real local A2A calls and observes the delayed write. No runtime/test implementation edits, paid calls, or Executor dispatch occurred during Planner QA.

## Scope review after three executions

**Preserve:** accepted R1/R2, provider-neutral CLI and adapters, saved-endpoint recovery, iteration/history safeguards, and the verified small-fixture replacement evidence. Do not reimplement the platform or rerun the full live comparison.

**Constrain:** A5-CANCEL-STARTUP fixes only worker startup/cancel ownership and terminal-state/lock ordering in server.py, with processes.py/store.py touched only if needed, plus focused real-process regressions. It must prevent launch after accepted cancellation or stop/reap a launched worker before releasing protection. Unknown stop state must retain recovery protection.

**Acceptance:** no post-cancel writes for either fake adapter; deterministic coverage of the spawn-registration interval; no overlap with a subsequent same-workspace task; honest worker-start/stop and round evidence; existing normal/cancel/deadline/restart and R1/R2 checks remain green.

The predecessor is archived as **superseded, not approved**, with all three executions and QA intact. The new task stays DRAFT until human approval and manual launch. No predecessor counters are reset. Full host-driven skill QA, Cursor invocation, third-party server compatibility, and broader performance claims remain disclosed limitations, not added scope for this fix. Documentation-only updates were handled directly by Planner.
