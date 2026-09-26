# A14 QA evidence — unreadable skill roots

Executor self-report, 2026-09-26. Branch `claude/a14-unreadable-skill-roots` from `main` at `69293fc`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass, as the first check inside `tests/smoke-manifest.sh`. A direct `bash -n bin/handoff` from this session was blocked by the command permissions configuration before the shell ran |
| `bash tests/smoke-manifest.sh` | 38 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q -m "not server"` | 141 passed, 109 deselected, 125.40s |
| `uv run --offline --extra test python -m pytest -q` | 250 passed, 712.97s (0:11:52), 100 warnings |
| `git diff --check` | clean |

The quiet suite ran in this session's sandbox. The full suite ran on this machine with process permissions, so the server-marked tests (localhost, process probes, a started service, a pseudo-terminal) actually ran. No test was skipped as a sandbox limit. The 100 warnings are existing `a2a` protobuf deprecation warnings in `tests/test_a2a_cli.py`. The Planner re-runs `pytest -m "not server"` in the real sandbox on the next QA.

`SKILL.md` is 193 lines.

## Criteria

1. **`status` keeps working.** With a scratch `HOME` whose `.cursor/skills` is mode `000`, `handoff status --json` exits 0. `workflow`, `execution`, `next`, `turn`, `status`, and `preflight` match the readable run. `planner_skill.unknown` is the `…/.cursor/skills/handoff-cli` path and `planner_skill.stale` is `[]`. The human text adds one line, `skill:  could not read <path> (permission denied); stale check skipped`, and the other lines match the readable run.
2. **`skill status`.** In that setup it exits 0. The Cursor user location is `state: "unknown"`, `probe: "not_permitted"`, `stale: false`, `unknown: true`. The human text includes `unknown (<detail>)`. The other locations stay as reported, with `unknown: false` and no `probe`.
3. **The unknown root is not a copy.** With only the Cursor root unreadable, a stale `elsewhere` copy under `.claude/skills` is still `planner_skill.stale`, and `any_skill_available` returns that copy. With no readable copy, `any_skill_available` is `None` and `init` prints both the install suggestion and `could not read <path>; the skill check skipped it`.
4. **`install` refuses.** `handoff skill install --host cursor --user` with an unreadable `.cursor/skills` exits 2, prints `cannot read <path> (Permission denied); not installing`, and leaves the directory empty. The repository git status is unchanged. `init --planner cursor` with the project `.cursor/skills` unreadable exits 0, prints that refusal as a warning plus the skipped-check line, and leaves the skill directory empty.
5. **Other errors.** A mocked `OSError` (`EIO`) from classification returns `unknown` with `probe: "error"`, `stale: false`, and `unknown: true`. `status_entries` does not raise.
6. **Unchanged when readable.** The A13 stale status test still expects an empty stale list and one stale `skill:` line. Readable JSON adds `planner_skill.unknown: []` and per-entry `unknown: false`. An execute-only skill root (mode `111`) is not reported absent: the entry is `unknown` with the root path. An unreadable child directory is skipped, and a readable `elsewhere` copy beside it is still found.
7. **Skill contract.** `SKILL.md` mentions `planner_skill.unknown` once, says the stale check skipped those locations, says that does not block anything, and says never to try to fix permissions. It does not mention `chmod`. `reference.md` documents `unknown`, `probe`, and that an unknown location does not block anything. Existing contract tests pass.
8. **Regression.** The quiet suite, the full suite, and the legacy smoke test passed. JSON outputs keep `urn:handoff-automation:cli-output:v1` and their existing fields. `planner_skill.unknown` and per-entry `unknown` are additive. The known A13 follow-up is removed; known follow-ups are "None open."

## Decisions

Mode `000` on `.cursor/skills` fails inside `skill_state`, on `…/skills/handoff-cli`, because Python 3.13 raises `PermissionError` from `is_symlink` / `exists`. That location path is what `planner_skill.unknown` lists. A root that can be stated but not listed (mode `111`) stays `absent` at the canonical path and becomes `unknown` with the root path when `iterdir` fails. An unreadable child is skipped.

`read_marker` still reports `foreign` when the directory can be read and only the marker cannot. Failures that prevent classifying the location, including `tree_digest`, are `unknown`.

Install refusal uses exit code 2, the same code as the other skill refusals. The human `status` line says `(permission denied)` when every unknown probe is `not_permitted`, and `(error)` otherwise. One path uses "skipped it"; more than one uses "skipped them".

`init --planner` warns through the existing skill-install error path when the project target is unknown. An unreadable user root does not block a project install into a folder that can be read.

`unknown: false` is added after `stale` on every skill-status entry, so a readable entry's other fields stay in place.

## Not done

No permissions were changed on a real home folder. The checks used a scratch `HOME` and a scratch project directory under pytest `tmp_path`.
