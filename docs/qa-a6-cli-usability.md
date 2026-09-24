# A6 CLI usability and Cursor: Executor evidence report

Task: A6-CLI-USABILITY-CURSOR. Branch `codex/a6-cli-setup-cursor-model-switch`,
started from `7ee6d788d30b594292f2c8a18fd25c6930e0d128`. This is the
Executor's self-report for independent Planner QA; it is **not** QA approval.

Durable evidence: [`evidence/a6-cursor-live-2026-09-24.json`](evidence/a6-cursor-live-2026-09-24.json)
(redacted; the service token was replaced before writing and no provider
credential was ever printed).

## What changed

| Area | Files |
|---|---|
| Public CLI | `bin/handoff`: `init` (guided on a TTY, scripted by flags, `--transport legacy` stays Python-free), `models`, `model`, `server`, `planner`; linked-worktree-safe excludes through `git rev-parse --git-path info/exclude`; DRAFT reported as the human's turn; EXECUTOR column in `runs`; the runtime is found through `HANDOFF_A2A_BIN`, then `PATH`, then this checkout's `.venv` |
| Setup | `src/handoff_a2a/setup.py`: generates `.handoff-config.json` (with a `managed` pointer), `.handoff-logs/server.json`, a 0600 token, excludes, the template, and the optional project-local Cursor skill. It checks tracked files, symlinks, and foreign skills before writing, writes atomically under a lock, rolls back on failure, preserves everything on re-init, and migrates explicitly |
| Discovery | `src/handoff_a2a/providers.py`: `cursor-agent models` / `status`, `codex debug models`, Claude "no enumeration"; argv-only; stable launcher paths |
| Service | `src/handoff_a2a/service.py`: owned child in its own session; verified through the Agent Card identity (workspace, provider/model, `config_generation`) plus an authenticated probe; conflict-safe port handling; stale records never signaled; stop refuses unresolved work; failed start stops only its own child and records the failure |
| Selection | `src/handoff_a2a/selection.py`: show / immediate switch (service.lock → submit.lock, quiescence, stop, publish, start, verify, restore on failure) / `--after-current` / `--cancel-pending`; plan `**Executor:**` constraint; `history.jsonl` |
| Planner | `src/handoff_a2a/planner.py`; `skills/handoff-cli/SKILL.md`; `templates/HANDOFF.md` roles |
| Cursor Executor | `src/handoff_a2a/adapters/cursor.py`; `adapters/base.py` optional `prepare_run`/`cleanup_run`; `server.py` three adapters, identity in results, delivery cleanup on every stop path |
| Dispatch boundary | `src/handoff_a2a/integration.py`: applies a queued selection before a new worker; snapshots the config and verifies the managed card identity under `submit.lock` (fixes the stale-config race); records the per-run executor identity; watch retries deferred dispatch; status shows selected/active/current-run/pending |
| Config | `src/handoff_a2a/config.py` (`managed` block), `workspace.py` (generated Cursor paths are workflow paths; decorated Branch values) |
| Tests | `tests/test_a2a_{setup,service,selection,cursor_planner}.py`; `a2a_harness.py` fake worker speaks Cursor and discovery; the provider matrix now includes Cursor for every existing roundtrip/lifecycle/CLI case |
| Live check | `scripts/a2a_cursor_live_check.py` |

Module mapping matches the plan; no consolidation was needed. No dependency
changes (`pyproject.toml`/`uv.lock` untouched).

## Automated verification

Run once after the final change (server/process tests need localhost
binding; this Executor's sandbox denies it, so the suite ran with local
binding permitted):

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass |
| `bash tests/smoke-manifest.sh` | 17 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q` | 157 passed (baseline at 7ee6d78: 99) |
| `./bin/handoff --help` | lists init/models/server/model/planner |
| `git diff --check` | clean |

Fake-provider coverage by acceptance criterion:

1. Fresh setup for all three providers through `init` + `server start`, token/dir modes, re-init preservation, missing auth, unknown model, and missing binary each write nothing (`test_a2a_setup`, `test_a2a_service`).
2. Bare re-init is nondestructive; a manual endpoint is preserved and then migrated explicitly (workspace ID kept, backup, external token untouched); paths with spaces; linked worktree; legacy init and DRAFT status Python-free.
3. Idempotent verified start, port conflict leaves the foreign listener alone, stale record never signaled, stop refuses unresolved work, failed start stops only its own child and blocks dispatch.
4. The fake Cursor run through public `handoff execute` receives the full HANDOFF in the rule, with an AGENTS.md present and the Planner skill installed; the rule is removed and untracked; manifest/`runs`/`status` show requested vs reported identity.
5. Every parametrized roundtrip/lifecycle/CLI case (success, nonzero, provider error, malformed, wrong shape, usage/cost, cancel, deadline, spawn-gate, duplicate/replay, restart, config switch) now also runs for Cursor.
6. The Planner launcher is interactive, cannot approve or dispatch, requires the skill and a listed model, and leaves a Codex Executor selection byte-identical.
7. Same-provider and cross-provider immediate switches within one workflow keep workflow ID, plan hash, round accounting, hold, branch/HEAD, and fingerprint.
8. `--after-current` during a slow worker, under a running `watch`: the first run stays on its model, the change applies exactly once, and the second run uses it. Cancel keeps the hold; unknown acknowledgement defers.
9. A held submit lock makes a switch refuse and execute defer; a stale generation is never dispatched to; a switch while a submission is recorded refuses; a failed new service restores and restarts the previous one; a failed queued change blocks dispatch until canceled.

## Live acceptance (paid; 5 of the 6-turn budget)

Versions: cursor-agent `2026.09.18-9a7762b`, codex-cli `0.156.0`, Claude Code
`2.1.281`. `cursor-agent models` listed 241 IDs for this account, including
Grok (`grok-4.7-*`, `cursor-grok-4.6-*`, `cursor-grok-4.5-high`). One
disposable fixture, one workflow `bf036cbe-7c4a-41b9-bec9-1d6728dd76fa`, and
only public commands for setup/service/selection/execution.

| Turn | Role / identity (requested → reported) | Outcome |
|---|---|---|
| P1 | Cursor CLI Planner, `claude-sonnet-5-thinking-high` → "Claude Sonnet 5 300K High"; Executor configured as **codex** meanwhile | Inspected the repo, checked for PRs, wrote a self-contained **DRAFT**, and stopped for approval. No approval, execution, or workflow record. It wrote ``**Branch:** `live/clamp` ``; the human review step removed the backticks before `handoff approve` (the product now accepts that form, see below). |
| — | `handoff model --provider cursor --model grok-4.7-high` | codex g1 → cursor g2; service stopped, restarted, and verified; workflow/approval/rounds unchanged |
| E1 | Cursor `grok-4.7-high` → "Grok 4.7 256K High" | COMPLETED, READY FOR QA, 208.6 s; commit `fixture: implement clamp …` (the AGENTS.md prefix rule was followed); **the Execution Notes contain the rule-only line `Handoff execution: 767eba1e-…`, so Cursor loaded the git-excluded delivery rule**; tests pass; the rule was removed afterwards; commit and tests ran inside Cursor's sandbox with shell network off |
| — | human QA 1 + `handoff model --provider cursor --model composer-2.5` | g3; unchanged workflow |
| E2 | Cursor `composer-2.5` → "Composer 2.5" | COMPLETED, 46.7 s; `ValueError` correction committed; tests pass; its own execution ID in notes |
| — | human QA 2 + `handoff model --provider codex --model gpt-5.6-luna --reasoning-effort medium` | g4; unchanged workflow |
| E3 | Codex `gpt-5.6-luna` (no reported model) | **FAILED delivery.** The code change was correct and committed (`fixture: export clamp …`, tests pass), but Codex appended its note to the end of the file, inside QA Feedback, so the server reported "executor changed Planner-owned plan or QA sections". Reconciliation restored `CHANGES REQUESTED`, set the dispatch hold, and consumed round 3/3. Not retried (rounds exhausted; no fresh workflow to evade it). |
| P2 | Same Planner session (`--resume`) | Reviewed `git diff main...live/clamp`, ran the tests (4 OK), and checked each QA item against the code. Moved the stray Executor note out of QA Feedback, wrote an actionable verdict, and set **APPROVED**. Launched no execution: manifest count stayed 3, and it ran no execute/approve/merge/push. |

Usage was reported by Cursor's `result.usage` (for example E1: 52,806 input,
4,039 output, 198,272 cache-read tokens); cost stays `null` for Cursor and
Codex.

### What the live check shows and does not show

- Shown: generated setup and a verified local service with no hand-written
  JSON or token; Cursor Grok and a second Cursor model editing, testing, and
  committing through `handoff execute`; delivery of the git-excluded HANDOFF
  plus AGENTS.md guidance with the Planner skill installed and no recursive
  dispatch; three immediate model switches inside one approved workflow,
  including cross-provider, with the service verified each time and
  per-run identities recorded; a Cursor CLI Planner drafting, stopping at
  approval, and later performing independent diff QA while the Executor was
  configured separately.
- The cross-provider **continuation delivery** did not pass: Codex edited the
  QA section. The switch itself, the correct code change, and the
  protection that refused to route it to QA all worked. This is a Codex
  Executor behaviour on this fixture (earlier A4 Codex live runs placed
  notes correctly). It is not an A6 transport defect, but acceptance 7's
  "cross-provider correction" is therefore only partly live-evidenced. The
  fake-worker coverage for criterion 7 passes.
- The Planner turns used `cursor-agent --print` with a restricted per-turn
  config, because a script cannot drive the interactive
  `handoff planner` session. The launcher's own command was recorded and
  tested separately.
- `--after-current` was exercised with fake workers only, as the plan allows.

## Changes made after the live run

1. The Cursor per-run config now also denies `Read(.handoff-logs/credentials/**)`,
   `Write(.handoff-logs/**)`, and `Write(.handoff-config.json)`. E1's
   workspace listing surfaced the token *path* (not its content). These
   extra rules are unit-tested but not yet exercised live.
2. `read_declared_branch` accepts a single Markdown code span
   (`` `live/clamp` ``), which is how the live Cursor Planner wrote it.

## Pending / not verified

- **Cursor editor skill discovery and invocation (pending, manual).** This
  session cannot drive the Cursor app. Step: run `handoff init "<repo>" --planner cursor`
  on a disposable repo, open it in the Cursor editor, check
  *Customize > Skills* lists `handoff-cli` (model-free), then in Agent chat
  type `/handoff-cli status` and confirm it runs `handoff status "<repo>"`.
  Only the Cursor **CLI** Planner has been exercised live.
- User-level Cursor content in the home directory (`~/.cursor/rules`,
  `~/.cursor/skills`, `~/.agents/skills`, and the MCP list) is not isolated
  from the Cursor Executor. See `docs/cli-setup-and-models.md`.
- No quality, speed, or cost claims.
