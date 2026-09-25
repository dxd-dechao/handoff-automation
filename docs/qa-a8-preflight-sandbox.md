# QA evidence — A8 preflight and sandbox

Executor self-report for `claude/a8-preflight-sandbox`. Independent Planner QA
still has to review the diff. Counts below are from this session.

## Checks

| Check | Result |
| --- | --- |
| `uv run --offline --extra test python -m pytest -q` | 203 passed, 100 warnings (proto deprecations), 2026-09-25 |
| `bash -n bin/handoff` | exit 0 |
| `bash tests/smoke-manifest.sh` | 24 passed, 0 failed |
| `git diff --check` | clean |

## Criteria

1. **Preflight.** `tests/test_a2a_preflight.py::test_preflight_lists_every_blocker` reports branch, dirty paths (not `HANDOFF.md`), plan hash, outstanding run, and stopped service together. `execute` prints the same list. Legacy `preflight` exits 1 with `preflight needs managed A2A` and a JSON error, without calling Python.
2. **Branch.** `test_first_execute_creates_a_missing_branch` creates `feature` from HEAD, prints it, and stores `branch_created` on the manifest. `test_existing_branch_is_not_switched_and_dirty_tree_is_untouched` leaves `main` and `dirty.txt` alone. `templates/HANDOFF.md` tells the Executor not to create or switch branches under A2A.
3. **init.** A foreign project skill is a warning (`test_collisions_refuse_and_roll_back`); config is kept and the foreign file is not overwritten. An elsewhere copy skips the project install (`test_skill_elsewhere_is_reported_and_not_modified`). An outdated copy at the project path is still upgraded on re-init (existing A7 test).
4. **Errors.** `test_permission_error_has_no_traceback` prints the path and `(permission denied; sandbox?)` as JSON, with no traceback.
5. **Liveness.** `test_ps_denied_is_unknown` and `test_started_server_outlives_the_launching_shell`. A dead PID stays stale (`test_stale_record_is_never_signaled`).
6. **Skill detection.** Elsewhere copy reports `found_path` and `current_content` and is not modified. An unrelated `name:` is ignored.
7. **Network vs login.** `failure_kind` maps `ECONNRESET` to `network` and authentication text to `auth`. `models --json` adds `error_kind` and includes `login_command` only for `auth`.
8. **Next steps.** `_next_steps` suggests `handoff skill install` only when `any_skill_available` is empty.
9. **Skill contract.** `test_skill_is_mode_aware_and_keeps_the_safety_rules` pins the sandbox section, both Claude settings, the trade-off, no `approve` / `Bash(handoff:*)` suggestion, unknown-as-sandbox, preflight before execute, init latency, and the existing A7 rules. SKILL.md is 200 lines.
10. **Connect and timeout.** `endpoint_unavailable` / `permission_denied` keep EPERM off the "server start" advice. Wait expiry prints timeout, execution id, "still in progress", and `handoff resume`, as text or `--json`, exit 2.
11. **Section boundaries.** `test_section_boundary_note_and_diff` accepts a removed `---` with a note and rejects a real plan edit with a diff that names the line. `test_approved_plan_hash_pin` locks `99f8cd04e18a04b4c2268d9804b1fc387cf3dc41923493896f7ca35c7b76ce9d`. The template and the Cursor delivery rule state the byte boundary. Codex `developer_instructions` carries the same sentence.
12. **Server lifetime.** `test_started_server_outlives_the_launching_shell`. Docs §4 say the service is not tied to the shell, with the host process-group caveat.
13. **Regression.** JSON objects keep `urn:handoff-automation:cli-output:v1`. Legacy status JSON adds `preflight: null`.

## Decisions

- An outdated copy at the project install path is still upgraded on re-init. Skip applies to a current copy and to a copy found under another folder (including a user-level copy).
- A running but unverified service is not a preflight blocker, so a generation mismatch still uses the existing "not running the selected Executor" deferral.
- `approved_plan_hash` is not normalized. Only the in-run delivery comparison ignores trailing whitespace, blank lines, and `---` lines.
