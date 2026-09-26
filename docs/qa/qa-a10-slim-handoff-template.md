# A10 — Slim handoff template

Executor self-report, 2026-09-25. Branch `claude/a10-slim-handoff-template`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | Passed |
| `bash tests/smoke-manifest.sh` | 28 passed, 0 failed (includes Python-free `template refresh`) |
| `uv run --offline --extra test python -m pytest -q` | 226 passed, 100 protobuf deprecation warnings, 600s. No paid model calls |
| `git diff --check` | Clean |
| `./bin/handoff --help` | Lists `template refresh` |

## Criteria

1. **Size and content.** `templates/HANDOFF.md` preamble is 2095 bytes (pinned ≤ 3072). It has the opt-in banner, Planner/Executor roles, `### Executor rules` (including the byte-for-byte boundary sentence), and `### Status values`. It has no Protocol, Drive mode, or Planner-rules sections.
2. **Skill coverage.** `test_skill_covers_planner_rules_removed_from_the_template` pins self-contained plans, open PRs, QA against the diff, actionable feedback (including nits), documentation-only corrections, and the three-execution policy.
3. **Hashes unchanged.** `test_template_preamble_is_slim_and_outside_the_hashes` shows `approved_plan_hash` and `planner_fingerprint` match for one Current Task under an old preamble and the new one.
4. **Refresh.** `test_template_refresh_keeps_the_task_and_refuses_unsafe_files` replaces only the preamble, keeps a backup, prints "already current" on the second run, and refuses a missing heading, a duplicated heading, and an outstanding run without writing. The smoke test repeats the happy path with `PATH=/usr/bin:/bin`.
5. **Init hint.** `test_init_names_refresh_only_when_the_preamble_differs` prints the refresh command for a stale preamble and omits it when the preamble matches. Both runs leave `HANDOFF.md` unchanged. Managed re-init goes through the same note in `setup.py`.
6. **Executor still works.** The fake-provider suite (including execute against a repo whose handoff came from the template path used by init) passed. `rule_text` names `### Executor rules`, which is the template heading.
7. **Waiting.** `test_skill_waiting_does_not_block_or_pipe` pins background execution, the wait-timeout wording, plain `handoff` commands, reading `reason`, no long foreground sleeps, and telling the human when a run starts, times out, and finishes. Existing skill contract tests passed.
8. **Unknown state.** `test_permission_denied_status_is_unknown_not_unresolved` reports `execution: UNKNOWN`, `probe: not_permitted`, the existing reason, turn `WAIT`, exit 2, and a next action that is not QA. A `ConnectionError` stays `UNRESOLVED`. `test_executor_active_state_is_unknown_when_probe_is_denied` sets `executor.active.state` to `unknown`.
9. **Follow-ups.** `docs/status/product-status.md` ends with `## Known follow-ups` and the five items from the task.
10. **Regression.** The counts above. JSON kinds keep `urn:handoff-automation:cli-output:v1`. Status JSON gains `probe` (null unless the probe was denied).

## Decisions

- `template refresh` treats `.handoff-logs/outstanding.json` as the outstanding / non-terminal run. That is the same signal for a READY FOR QA file whose run has not finished. The command stays in bash and does not call the A2A runtime.
- The skill stays at 180 lines (limit 200) by putting the new waiting rules on fewer, longer lines.
- This repository's own `HANDOFF.md` preamble was not refreshed. That is the Planner's follow-up after merge.
