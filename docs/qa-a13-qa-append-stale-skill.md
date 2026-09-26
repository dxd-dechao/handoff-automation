# A13 QA evidence — QA append and stale skill copies

Executor self-report, 2026-09-26. Branch `claude/a13-qa-append-stale-skill` from `main` at `a7d9422`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass (also the first smoke check) |
| `bash tests/smoke-manifest.sh` | 38 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q -m "not server"` | 135 passed, 109 deselected, 115.24s |
| `uv run --offline --extra test python -m pytest -q` | 244 passed, 704.50s (0:11:44) |
| `bash bin/handoff --help` | `qa` help says `--append` from READY FOR QA appends a round and sets Status |
| `git diff --check` | clean |

The full suite was run on this machine with process permissions. It includes the server-marked tests (localhost, process probes, a started service, a pseudo-terminal). The `not server` count above is from the same checkout. No test was skipped as a sandbox limit. The Planner re-runs `pytest -m "not server"` in the real sandbox on the next QA.

`planner_skill_report` on this checkout, using the real home directory, five times after one warmup: min 1.2 ms, median 1.3 ms, max 1.6 ms. That is the hashing `handoff status` adds. It is under the 50 ms note. The run listed the three existing skillshare copies and did not modify them.

## Criteria

1. **Append plus Status.** On READY FOR QA with a round-1 body, `handoff qa --append --status APPROVED` keeps that body, appends round 2 after one blank line, sets Status to APPROVED, leaves `approved_plan_hash` unchanged, and writes one pre-QA backup. The output says both "appended QA Feedback" and "Status set to APPROVED". `--append` on APPROVED leaves Status unchanged. `--append` from DRAFT and from READY FOR EXECUTION exits nonzero and leaves the file unchanged. The existing CRLF and mode tests still pass.
2. **`skill status` stale marking.** A user-root copy under another folder name, with different content, is `state: "elsewhere"`, `current_content: false`, `stale: true`. The text includes the stale mark and "update it with the tool that installed it (e.g. skillshare); handoff never modifies it". A current `elsewhere` copy is `stale: false`. An `outdated` project copy is `stale: true`. Tree digests of those copies match before and after `skill status`.
3. **`init` next steps.** With only a stale `elsewhere` copy for `--planner claude`, init names that path and the update hint, does not suggest `handoff skill install`, and does not create `.claude/skills/handoff-cli`. The copy's tree digest is unchanged. A current `elsewhere` copy produces no stale line.
4. **`status`.** With no stale copy, `planner_skill.stale` is `[]` and the human text has no `skill:` line. With the stale user copy, `planner_skill.stale` is that path and there is one `skill:` line. Existing status fields stay. Preflight text and JSON are the same before and after the stale copy is added, and the JSON has no `planner_skill`.
5. **Skill contract.** `SKILL.md` is 191 lines. It uses `--append` for a later QA round, reports a stale copy with the skillshare update hint, says never to edit an `elsewhere` copy, and says to mention `planner_skill.stale` once at the start of a conversation. Existing contract tests pass.
6. **Regression.** The quiet suite, the full suite, and the legacy smoke test passed. `bash -n bin/handoff` passed. `git diff --check` is clean. JSON outputs keep `urn:handoff-automation:cli-output:v1` and their existing fields. `stale` on skill-status entries and `planner_skill` on A2A status are additive.

## Decisions

`cmd_qa` already set Status on `--append` when Status was READY FOR QA. The refusal is only for other statuses, except `--append` on APPROVED or CHANGES REQUESTED, which still leaves Status unchanged. A comment records that. The new test is what pins it.

Legacy `handoff status` stays Python-free and does not include `planner_skill`. Managed A2A status does. `handoff skill status` reports `stale` whenever the runtime is present, including for a legacy repo.

An init line for a stale `elsewhere` copy uses the skillshare hint and does not name `handoff skill install`. An init line for a stale `outdated` copy that init did not already upgrade names that copy's existing upgrade command.

When every stale copy is `elsewhere`, the human `status` line includes the skillshare hint. When a stale copy is `outdated`, that line does not say handoff never modifies it.

## Not done

The three stale skillshare copies on this machine were not updated, synced, or deleted.
