# QA evidence — A9 fingerprint rebaseline

Evidence for `claude/a9-fingerprint-rebaseline`, from `main` at `78ab868`.
Executor self-report; independent Planner QA is pending.

## Checks

| Check | Result |
| --- | --- |
| `uv run --offline --extra test python -m pytest -q` | 219 passed, 100 warnings (proto deprecations), 2026-09-25 |
| `bash tests/smoke-manifest.sh` | 24 passed, 0 failed (includes `bash -n bin/handoff`) |
| `git diff --check` | clean |

## Criteria

1. **Bytecode ignored.** `tests/test_a2a_workflow.py::test_untracked_bytecode_is_not_code` adds, changes, and deletes untracked `pkg/__pycache__/m.cpython-312.pyc` and `x.pyc` in a fixture whose local excludes file is empty. `code_fingerprint()` stays put, and `dirty_code_paths()` does not list them. Porcelain still shows the files, so they are not hidden by gitignore.
2. **Tracked bytecode.** `test_tracked_bytecode_still_counts` commits `vendor.pyc` and then modifies it. The fingerprint changes and the path is dirty.
3. **Hash stable.** The same test compares a clean tree with the pre-A9 payload (workflow paths skipped, bytecode not special-cased). The hashes match.
4. **Field case.** `tests/test_a2a_preflight.py::test_bytecode_baseline_and_reapprove_rebaseline` approves, runs the fake Executor in `bytecode` mode, deletes the bytecode, and preflight is ready. A legacy mismatch is written with `save_workflow`. Preflight then reports code `fingerprint`, the original message, and a fix that says to revise Current Task and re-approve. `handoff approve` prints `rebaselined code snapshot (previous ffffffffffff)`. `workflow.json` has the new fingerprint, `previous_post_run_fingerprint`, and `rebaselined_at`. Preflight passes. `workflow_id` and `rounds_used` stay the same.
5. **Unchanged plan.** That test re-approves without editing the plan: no rebaseline line, and `post_run_fingerprint` is unchanged. `test_rebaseline_on_changed_plan_only` covers the same rule in `workflow.approve`. The early return that refuses reapproval while work is unresolved is untouched.
6. **Dirty refusal.** `test_revised_approve_refuses_a_dirty_tree` names `app.py` and says to commit or discard. `workflow.json` and the Status line are unchanged.
7. **Other approvals.** A first approval still has a null baseline (`test_rebaseline_on_changed_plan_only`, and the field-case test before execute). `test_approval_round_and_supersede` still shows the successor inheriting the parent fingerprint.
8. **Regression.** 219 tests and 24 legacy checks passed. JSON outputs keep `urn:handoff-automation:cli-output:v1` and their existing fields. New receipt fields are only on `workflow.json`.

## Decisions

- `approve(..., current_fingerprint=None)` does not rebaseline. The CLI always passes the current fingerprint, branch, and dirty paths. `workflow.py` does not call git.
- Rebaseline runs only for an existing workflow whose plan hash changed, which already has `post_run_fingerprint`, and which has no reserved round.
- There is no workflow history helper. Per-run events and the service `history.jsonl` are different logs. No new log file was added.
- `skills/handoff-cli/SKILL.md` and `reference.md` do not describe fingerprint recovery or approve output, so they were not edited.
