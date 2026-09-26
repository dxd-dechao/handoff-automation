# A11 — Handoff integrity

Executor self-report, 2026-09-25. Branch `claude/a11-handoff-integrity`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | Passed (also the first smoke check) |
| `bash tests/smoke-manifest.sh` | 33 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q` | 231 passed, 100 protobuf deprecation warnings, 629s. No paid model calls. The QA command test was re-run after the suite and passed |
| `git diff --check` | Clean |
| `./bin/handoff --help` | Lists `qa` |

## Criteria

1. **Structure.** Fake-executor modes `duplicate_heading`, `second_current_task`, `drop_execution_notes`, and `reorder_sections` are each rejected, and the manifest reason names the heading. A Current Task that quotes `` `## QA Feedback` `` inline is accepted. A CRLF file is judged like its LF form. The A8 separator-only delivery still passes. The pinned A8 plan hash is unchanged.
2. **`handoff qa`.** On a READY FOR QA file that quotes the heading inline, the command writes the real QA section, sets Status, leaves `approved_plan_hash` unchanged, keeps mode `0640` and CRLF, and saves `HANDOFF.pre-qa-<UTC>.md`. `--append` on APPROVED adds text and leaves Status unchanged. It refuses, without writing, while a run is outstanding, from DRAFT and READY FOR EXECUTION, and on a malformed file. JSON `kind` is `qa` with `urn:handoff-automation:cli-output:v1`. Legacy smoke refuses with the hand-edit message.
3. **Archive.** A2A `archive` and `archive --superseded` refuse a changed plan and a duplicated heading, name `.handoff-logs/<run_id>-delivered.md` and `HANDOFF.pre-qa-*.md`, and leave `HANDOFF-ARCHIVE.md` and `workflow.json` unchanged. A well-formed APPROVED file still archives. Legacy smoke refuses a duplicated `## QA Feedback`.
4. **Delivered copy.** An accepted fake run and each rejected heading run leave `.handoff-logs/<run_id>-delivered.md` equal to the server evidence `handoff-delivered.md`, and the manifest `delivered_handoff_sha256` matches those bytes. Reconcile with a missing server copy records `delivered_handoff: null` and returns.
5. **Skill.** Contract tests pin `handoff qa` for A2A writes, the `plan_changed_since_approval: false` check, legacy hand edits with headings matched at the start of a line, and the watch rule that a background `handoff watch` starts only when the host supports long-lived background commands and the human agrees. `SKILL.md` is 182 lines. The preamble is 2140 bytes.
6. **Regression.** The counts above. `.handoff-logs/` was already excluded through `git info/exclude`; no new exclude was added. No fixture needed a heading change.

## Decisions

- `approved_plan_hash` and `planner_fingerprint` raise when `## Current Task` or `## QA Feedback` appears more than once, which is the first-or-last choice. A file that is only missing a heading is still rejected by `handoff_structure`, and the pinned one-section hash string still hashes as before.
- `status` reports structure problems in `reason` and does not crash. `plan_changed_since_approval` is the hash comparison archive uses; an unreadable duplicate does not pretend the hash matched.
- No existing fixture needed changing. Quoted headings count only as a whole line, so `###` headings inside Current Task stay valid.
