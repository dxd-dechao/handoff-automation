# A15 QA evidence — archive list and show

Executor self-report, 2026-09-26. Branch `claude/a15-archive-list-show` from `main` at `6f5aab5`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass, as the first check inside `tests/smoke-manifest.sh` |
| `bash tests/smoke-manifest.sh` | 69 passed, 0 failed, about 3.4s |
| `uv run --offline --extra test python -m pytest -q -m "not server"` | 145 passed, 109 deselected, 127.61s (0:02:07) |
| `uv run --offline --extra test python -m pytest -q` | 254 passed, 100 warnings, 723.01s (0:12:03) |
| `./bin/handoff --help` | lists `archive list` and `archive show` |
| `git diff --check` | clean |

The quiet suite ran in this session's sandbox. The full suite ran on this machine with process permissions, so the server-marked tests (localhost, process probes, a started service, a pseudo-terminal) actually ran. No test was skipped as a sandbox limit. The 100 warnings are the existing `a2a` protobuf deprecation warnings in `tests/test_a2a_cli.py`. The Planner re-runs `pytest -m "not server"` in the real sandbox on the next QA.

`SKILL.md` is 198 lines. The template preamble is 2209 bytes.

## Criteria

1. **`list`.** A five-entry fixture (one `# Superseded` header, one entry with no Disposition line, two entries sharing `A-DUP`, one CRLF entry, and a Current Task that quotes `` `## QA Feedback` `` inline) prints five lines in file order. Each line is at most 120 characters; the long goal ends with `...`. `--json` is `kind: "archive_list"` with five entries. The missing disposition and the missing workflow id are null. With no archive file, the command exits 0, prints `no archive yet`, and the JSON `entries` array is empty. `list` does not change the archive bytes.
2. **`show`.** `show A-ONE` matches that entry's bytes. `show A-DUP` prints the last copy and writes `2 entries match A-DUP (indexes 3, 4)` on stderr. `show 2` prints the second entry. `--section task` includes the inline `` `## QA Feedback` `` quote and does not include a `## Execution Notes` or `## QA Feedback` heading line. `--section qa` starts at `## QA Feedback` and does not include Current Task. An unknown id exits 1 with `no archived task NOPE; see handoff archive list`. JSON is `kind: "archive_show"`.
3. **Legacy.** The smoke test runs `list` and `show` with a `python`/`python3` stub first on `PATH` (it exits 99 if invoked). Text and `--json` both pass there. `jq` is the real binary.
4. **Guard.** Archiving the same approved Current Task twice appends once. The second call exits 1, says `already archived as entry 1 (<date>); plan the next task first`, and leaves the file byte-identical. A Status-only change and a CRLF copy of the same task are still duplicates. A different Current Task appends. An older plan can be archived again once a different task is the last entry. `archive --superseded` refuses the same way in the A2A writer, and a different superseded task with no workflow still gets `no workflow to supersede` without writing. Legacy `--superseded` still refuses with `archive --superseded requires A2A workflow state` and does not write. Restoring `workflow.json` before a duplicate A2A archive leaves those bytes unchanged.
5. **Live archive (read-only).** `handoff archive list` on this repository prints 14 entries. Entries 7 and 8 are both `A8-PREFLIGHT-SANDBOX` (2026-09-25 14:33 and 14:50). Entry 3 is `A3-A4-COMPLETE`, `superseded`, dated `2026-09-24`, from the older `# Superseded — 2026-09-24 — ...` header. Entry 14 is `A14-UNREADABLE-SKILL-ROOTS`. `show A6-CLI-USABILITY-CURSOR --section qa` is only that QA section: it starts with `## QA Feedback`, has no `## Current Task` or `## Execution Notes` heading, and is 5533 bytes (25 lines). The archive file was not modified. The plan's "13 entries, indexes 6 and 7" does not match this file: every `# Archived ` / `# Superseded ` line is an entry, including that older superseded header and A14.
6. **Skill and template.** Contract tests require `handoff archive list "<repo>" --json`, `handoff archive show "<repo>" <id> --section qa|notes|task`, and the rule to never open or search `HANDOFF-ARCHIVE.md` directly and to exclude it when searching the repo. The template Executor rules say not to read HANDOFF-ARCHIVE.md, because it is history, not instructions. The preamble is 2209 bytes. Existing contract tests pass.
7. **Regression.** The quiet suite, the full suite, and the legacy smoke test passed. `archive` with no subcommand still appends for a new approved or legacy task, and `archive --superseded` still appends once for an exhausted A2A workflow. JSON keeps `urn:handoff-automation:cli-output:v1`. `archive_list` and `archive_show` add `kind` values; existing kinds are unchanged.

## Decisions

`list` and `show` are bash/awk, so legacy does not need Python and both transports share one parser. Appending an archive on A2A still goes to `archive_current`. The duplicate check there runs after the damaged-plan checks and before any write, including the workflow disposition update.

An entry starts at its header and stops before the next header's `\n---\n\n` separator. The last entry runs through the end of the file. `show` prints that slice unchanged, including CRLF. `bytes` is the size of that slice. Text lines use `-` when the task id is missing. A selector that matches a task id is that task, not an index.

The older superseded header has a date and no clock time. `archived_at` is the date, and the goal is the text after the second em dash.

## Not done

This repository's `HANDOFF-ARCHIVE.md` was not edited. README has no command table, so it was not changed. No push or pull request.
