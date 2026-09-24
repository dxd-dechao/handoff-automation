# A7 skill-first setup: Executor evidence report

Task: A7-SKILL-FIRST-SETUP. Branch `codex/a7-skill-first-setup`, started from
`2a5bf25` (the human's pending README restructure and `docs/advanced-setup.md`
were committed first, as `2d37097`). This is the Executor's self-report for
independent Planner QA; it is **not** QA approval.

Durable evidence: [`evidence/a7-skill-setup-live-2026-09-25.json`](evidence/a7-skill-setup-live-2026-09-25.json)
(the service token never appeared in any output; checked by value).

## What changed

| Area | Files |
|---|---|
| Run mode | `config.py` (`managed.run_mode`, validated), `selection.py` (`handoff mode`, `run_mode`, `set_mode`; history event), `setup.py` (`init --mode`, re-init mode change, TTY run-mode question, mode-aware next steps), `integration.py` (`mode:` / `watcher:` status lines, mode-aware `next:`, watch refusal in drive, poll-boundary exit after a switch to drive), `service.py` (watcher record: PID + start identity, stale records ignored, second watcher refused) |
| JSON output | `reporting.py` (`urn:handoff-automation:cli-output:v1` helpers); `--json` on `status` (A2A in Python, legacy in `jq`), `models`, `model` (show and change), `server start/status/stop`, `mode`, `skill install/status` |
| Skill installer | new `skills.py` (one installer for cursor/codex/claude project folders and explicit `--user`; `.handoff-skill.json` marker; absent/current/outdated/foreign/unsupported; in-place upgrade of unmodified copies; A6 unmarked digest recorded); `handoff skill`; `init --planner <cursor|codex|claude>`; `workspace.py` excludes all three project paths from fingerprints; `planner.py` notes an outdated copy |
| Executor isolation | `adapters/claude.py` and legacy `bin/handoff` execute pass `--disallowedTools` for the handoff CLIs and `Skill(handoff-cli)`; `adapters/codex.py` also disables a project `.agents/skills/handoff-cli` |
| Public CLI | `bin/handoff`: skill-first `--help`, `skill` and `mode` dispatch (legacy `mode` answer is Python-free), `status --json`, `[A2A/legacy]` TTY prompt (Enter = A2A), `--mode` / `--planner` hosts, legacy `--mode` refusal |
| Skill | `skills/handoff-cli/SKILL.md` (193 lines, by intent) and new `reference.md` |
| Template / docs | `templates/HANDOFF.md` drive text; README (skill-first); `docs/cli-setup-and-models.md` (full manual reference); `docs/advanced-setup.md`; this report; status docs |
| Tests | new `test_a2a_skill_install.py`, `test_a2a_run_mode.py`; additions to `test_a2a_{adapters,cli,contracts,setup,cursor_planner}.py`; 4 new legacy smoke checks |
| Live check | new `scripts/a2a_skill_setup_live_check.py` |

No dependency changes (`pyproject.toml` and `uv.lock` untouched).

## Decisions and deviations

1. **Claude deny rule is per launch, not in `.claude/settings.local.json`
   (deviation from the plan's wording).** The plan asked for the `handoff`
   deny rule in the Claude allowlist template and the generated
   `settings.local.json`. That file is also read by a Claude Code Planner in
   the same repository, so a deny there would stop the Planner from running
   `handoff`, which breaks skill-first use with `--host claude`. The same
   rules go on the Claude Executor's launch instead (`--disallowedTools`,
   verified in `claude --help` 2.1.281): the A2A adapter and legacy execute
   share one list (a test compares them), and the template and settings merge
   stay unchanged. Existing projects get it with no re-init.
2. **One new module** (`skills.py`). The JSON helpers live in `reporting.py`
   and `handoff mode` in `selection.py`.
3. **Watcher record** is written only by the A2A (Python) watch. Legacy watch
   stays a Python-free Bash loop with no record; legacy has no run mode.
4. **Watch start in drive mode** is refused by the Python watch (exit 2). A
   watcher refuses to start while another verified watcher for the same repo
   is running.
5. **`--user` paths** follow each host's docs: `~/.cursor/skills`,
   `~/.agents/skills` (Codex, verified in A4), `~/.claude/skills`. Claude Code
   does not document whether a relocated `CLAUDE_CONFIG_DIR` changes that
   folder, so `--user --host claude` is refused while it points elsewhere.
   Cursor also loads `.agents/skills` and `.claude/skills` project folders
   ([Cursor docs](https://cursor.com/docs/skills)); its per-run deny rules
   cover all of them.
6. **Legacy `init --planner`** uses the Python installer when the runtime is
   present, and otherwise makes a fresh, Python-free copy only (no upgrade).
7. **Invalid `run_mode`** in a hand-edited config fails every config-loading
   command with the reason; `status` still reports without a mode line.

## Automated verification

Run on the final tree (server/process tests need localhost binding; this
Executor's sandbox denies it, so the suite ran with binding permitted):

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass |
| `bash tests/smoke-manifest.sh` | 21 passed (17 existing + legacy deny flag, legacy `status --json`, `mode` refusal ×2) |
| `uv run --offline --extra test python -m pytest -q` | 192 passed (baseline at 2a5bf25: 157) |
| `./bin/handoff --help` | leads with `handoff skill install` and `handoff watch` |
| `git diff --check` | clean |

Fake-provider coverage by acceptance criterion:

1. Bootstrap: `test_a2a_skill_install` covers each host's project install
   (marker, exclude, fingerprint exclusion, idempotent re-run); an outdated
   marked copy and an unmarked known-release copy upgrade; modified,
   unrelated, symlinked, and tracked copies are refused unchanged, with the
   way forward; `--user` for each host writes only the user folder; Claude
   `--user` with a relocated `CLAUDE_CONFIG_DIR` is refused; `init --planner
   codex|claude` installs and re-init upgrades; the A6 digest matches `c9f97b9`.
2. Run mode: `test_a2a_run_mode` covers `init --mode`, `handoff mode`
   show/set/JSON, and re-init `--mode`. A mode change leaves `workflow.json`,
   HANDOFF.md, HEAD, and `server.json` byte-identical. A6-era configs and
   legacy repos report "not set". The legacy paths are Python-free
   (`PATH=/usr/bin:...`, no runtime).
3. Enforcement: drive refuses watch with the fix command. A running watcher
   with a slow fake worker exits cleanly after a switch to drive, once its
   in-flight run is reconciled: one launch, `completed`, READY FOR QA, record
   removed. In watch mode, `execute` clears a hold. Stale and garbage records
   are ignored, and a live one is reported and blocks a second watcher.
4. JSON: `test_json_outputs_for_models_model_and_server` and the run-mode
   tests cover schema/kind on success and error paths and that the token
   value is absent. Legacy `status --json` works with no Python. The TTY
   `init` accepts Enter as A2A (pty test). Existing non-interactive and
   human-output tests pass unchanged, except the new TTY run-mode answer
   and the `handoff planner` missing-skill message (it now names
   `handoff skill install`).
5. Executor isolation: `test_executor_runs_cannot_use_the_planner_skill_in_any_location`
   runs claude, codex, and cursor through a real `handoff execute`, with the
   skill in all three project folders and all user folders. Claude's argv has
   the deny list, Codex's `skills.config` disables the project and user
   copies, and Cursor's per-run config denies `handoff`/`handoff-a2a`. Unit
   tests pin the list and the legacy copy.
6. Skill contract: `test_a2a_contracts` pins the rules rather than the prose:
   - checks existing state with `--json` first;
   - proposes A2A as a yes/no, requires explicit agreement, and falls back to
     legacy with its limits;
   - offers migration;
   - asks provider, real model IDs, Codex-only effort, run mode, and skill
     location;
   - never chooses for the human;
   - ends the turn on the questions;
   - is mode-aware after approval;
   - keeps the DRAFT, self-approval, three-run, merge, and Executor rules;
   - uses only real CLI verbs;
   - links `reference.md`.
7. Docs: every `handoff` line in the README and CLI guide code blocks uses a
   real verb and real flags (`test_documented_commands_use_real_verbs_and_flags`).

## Live acceptance (paid; 4 of the 4-turn budget)

Cursor CLI Planner `claude-sonnet-5-thinking-high` (reported "Claude Sonnet 5
300K High"), print mode, cursor-agent `2026.09.23-86fc751`, one resumed session
per run, on fresh disposable fixtures. The scripted human answered: A2A yes,
Executor `cursor / composer-2.5`, watch. Bootstrap was `handoff skill install
--host cursor`. The Planner's sandbox was off (setup needs network and a
local port); dispatch, approval, publishing, and nested agents were denied.

| Turn | Request | Result |
|---|---|---|
| 1 (run 1, S1) | "set up handoff in this repo" | **Failed (recorded).** The Planner tried the structured question tool, which returns nothing in print mode, read that as "no selections came through", and ran `init --transport a2a --executor cursor --model gpt-5.6-sol-medium --mode drive` plus `server start` with its own choices. Fixed in the skill: the questions end the turn, an empty question result is not an answer, and no default fills a gap. A contract test pins it. |
| 2 (run 2, S1) | same, fresh fixture | Checked `status/models/skill status --json`, then asked in text: transport (A2A recommended, or legacy with its limits), provider (with readiness), model (real IDs), Codex-only effort, run mode. It ran no setup command. |
| 3 (S2) | the answers | Ran `handoff init … --transport a2a --executor cursor --model "composer-2.5" --mode watch`, `server start`, and `server status --json`. Reported cursor / composer-2.5, watch, service verified, and gave `handoff watch "<repo>"`. The script confirmed `server status --json` verified, only a cursor block, `run_mode: watch`, and no workflow or run. |
| 4 (S3) | "plan <clamp task> in the handoff" | Checked status, wrote a DRAFT (branch `live/clamp`), and stopped for approval. There was no approval receipt, execution, or watcher, and code/HEAD were unchanged. |

All 13 scripted checks of run 2 passed. Observations:

- S1 phrased transport as "A2A (recommended) or legacy?", not the literal
  yes/no; it asked for the choice and did not assume it.
- S1 reported Claude as "not logged in". `models --json` had said
  `"available": false` (Claude cannot list models) next to a
  `login_command`. The field is renamed `models_listed`, and the skill and
  reference say only an `error` means missing or logged out. This rename and
  the turn-boundary rule came after, or during, the live runs and are not
  re-verified live; the budget is spent.

**Pending: Cursor Editor discovery and invocation.** Not evidenced. Manual
step:
1. In a repo prepared with `handoff skill install "<repo>" --host cursor`,
   open it in the Cursor Editor and confirm *Customize > Skills* lists
   `handoff-cli`.
2. In an Agent-mode chat, send `/handoff-cli set up handoff in this repo`.
   Expect one round of questions (AskQuestion) and no `handoff init` until
   you answer.

## Limits

- Live evidence covers the Cursor CLI Planner in print mode only. Codex and
  Claude Code Planner hosts are covered by installer and fake-worker tests,
  not live turns.
- The Claude deny rule relies on Claude's `--disallowedTools` matching,
  including `*/handoff` patterns. It was not exercised against a live Claude
  Executor (no Executor run was in scope).
- Watchers are Python (A2A) only. A legacy watch has no ownership record.
