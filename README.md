# handoff-automation

Removes the human courier from the two-agent HANDOFF.md workflow
(PLANNER in Cursor / Claude Code desktop, EXECUTOR = headless Claude Code
CLI on a separate account) while keeping:

- separate accounts for planner and executor,
- fresh executor context on every run (`claude -p` starts cold),
- **HANDOFF.md as the only channel between the agents**,
- human gates for plan approval and merging.

## Setup

1. **Executor account.** The `claude` CLI (`~/.local/bin/claude`) must be
  logged into the executor account (run `claude /login` once in a terminal).
   The planner runs in Cursor / the desktop app on its own account; this tool
   never touches it.
2. **PATH.** Add this repo's `bin` to your PATH, e.g. in `~/.zshrc`:
  ```sh
   export PATH="$HOME/Documents/handoff-automation/bin:$PATH"
  ```
3. **Install the protocol into a target repo:**
  ```sh
   handoff init ~/path/to/repo
  ```
   This copies `templates/HANDOFF.md` to the repo root and adds `HANDOFF.md`,
   `HANDOFF-ARCHIVE.md`, and `.handoff-logs/` to `.git/info/exclude` —
   local-only excludes, deliberately **not** `.gitignore`, because the handoff
   machinery is this machine's workflow, not part of the project.
4. **Executor permission allowlist.** `handoff init` installs this
   automatically; for a repo that was init'ed earlier, run:
  ```sh
   handoff permissions ~/path/to/repo
  ```
   This **merges** a `permissions` block into the repo's
   `.claude/settings.json` — existing hooks, sandbox config, and permission
   entries are preserved (safe to re-run; it dedupes). The Bash rules adapt
   to the repo's package manager, detected from its lockfile
   (`yarn.lock` / `pnpm-lock.yaml` / `package-lock.json`).
   What it grants and why:
  - **allow:** `Edit`/`Write` (implementing the plan), `cd` (scripts often
  live in subpackages — `cd backend && yarn test` needs every part allowed),
  `git add/commit/status/diff/log` (commit-as-you-go on a branch), and the
  detected package manager's `test/lint/check/typecheck/build/pre-commit/format`
  scripts plus `npx tsx/vitest/tsc/eslint` (running the acceptance checks
  itself). The script list is a fixed safe list on purpose — repo-specific
  extras (a `migration:generate`, a `db:reset`) are a human decision; add
  them to `.claude/settings.json` by hand.
  - **deny:** `git push`, `git merge`, `gh pr create`. Publishing requires
  explicit human authorization written into the task plan — and even then a
  human runs it, or temporarily widens the allowlist for that one task.
   `templates/executor-settings.json` remains as a reference for the pnpm
   shape of this block. Since `.claude/settings.json` is usually committed,
   review the merge with `git diff` and commit it — the executor account
   reads the same file.



## The loop

Two ways to run it. Both keep the same two human gates (plan approval,
merge) and the same channel rule (agents talk only through HANDOFF.md).

**Drive mode — everything happens in the planner chat:**

```
 1. you → planner chat: "plan <task> in the handoff and drive it"
       PLANNER runs `handoff archive`, writes the plan, asks you to approve

 2. ★ HUMAN GATE (a chat reply): "approved"

 3. PLANNER runs `handoff execute` with its own shell tool
       executor implements on its own account, sets READY FOR QA

 4. PLANNER QAs the git diff
         CHANGES REQUESTED → back to 3, automatically (stops after 3 rounds)
         APPROVED          → step 5

 5. ★ HUMAN GATE: you merge; back to 1
```

The planner session must stay open while the executor runs (the executor is
its child process in this mode). The account boundary survives because
`handoff execute` strips the planner's auth environment before launching the
executor.

**Watch mode — planner chat + a terminal you leave open:** run
`handoff watch [repo]`. It auto-executes executor turns and posts a macOS
notification on READY FOR QA / APPROVED; you relay "QA the handoff" to the
planner chat yourself. Use this when a task is long and you don't want to
keep a chat session open — at the cost of the one relay phrase per QA round.

Don't run both modes on the same repo at once; a per-repo lock makes the
second `execute` fail loudly rather than double-run the executor.

Day-to-day:

```sh
handoff status [repo]    # whose turn is it, what's the task
handoff watch  [repo]    # leave running: auto-executes executor turns,
                         # desktop-notifies on planner/human turns
handoff execute [repo]   # one executor run, manually
handoff runs   [repo]    # list past executor runs (from manifests)
handoff archive [repo]   # snapshot the finished task before PLANNER
                         # writes the next plan
```

`watch` polls the `Status:` line (default 30s, `HANDOFF_POLL_INTERVAL` to
change). On `READY FOR EXECUTION` / `CHANGES REQUESTED` it runs the executor;
on `READY FOR QA` / `APPROVED` it posts a macOS notification and keeps
watching. If an executor run fails without changing the status, watch does
**not** retry until the status line changes — fix the problem, then touch the
status (or run `handoff execute` by hand).

### Audit trail

Every `handoff execute` run produces three files in `$repo/.handoff-logs/`
(git-excluded), keyed by a run ID (`YYYYMMDD-HHMMSS-PID`):

| File | Contents |
|------|----------|
| `<run_id>-execute.log` | Human-readable log: header, executor's final result text, stderr, and closing summary |
| `<run_id>-result.json` | Raw JSON output from `claude --output-format json` (verbatim) |
| `<run_id>-manifest.json` | Structured manifest: durations, tokens, cost, exit code, status transition, git commits with GitHub URLs |

Key manifest fields: `run_id`, `trigger_status`/`status_after`, `started_at`/
`finished_at`, `wall_duration_s`, `exit_code`, `git.commits[]` (each with
`sha`, `subject`, `url`), `claude.total_cost_usd`, `claude.usage.*`.

`handoff runs [repo]` lists past runs in a columnar format from their
manifests.

**Dependencies:** `jq` (1.7+) is required for `execute` and `runs`.

**Note:** With `--output-format json` there is no live streaming output during
execution. The executor's result text is written to the log file at the end of
the run.

## Invocation discipline

The executor is always invoked with the fixed phrase
**"execute the handoff"** and nothing else. `bin/handoff` deliberately has no
flag, argument, or env var to add instructions to the prompt. If you want the
executor to know something, write it into HANDOFF.md — that keeps the file
the complete, auditable contract, and keeps every executor run reproducible
from the file alone. The same discipline applies to the planner side: "plan …
in the handoff", "QA the handoff", "drive the handoff". Drive mode changes
who types the ritual phrase, not the channel — the planner invokes the
executor only through `handoff execute`, which cannot carry extra
instructions.

## Limitations (intentional)

- **The executor runs headless (**`claude -p`**), so it cannot answer permission
prompts.** Anything outside the allowlist simply fails. That is the point:
the allowlist is the executor's authority boundary, and widening it is a
human decision, not something an agent can talk its way through.
- `--permission-mode acceptEdits` auto-accepts file edits only; shell commands
still go through the allowlist.
- `watch` is a poller, not a daemon: it runs in a terminal you leave open, and
one repo per invocation.
- One executor run per repo at a time, enforced by a lock directory
(`.handoff-logs/execute.lock`). If a run crashes and leaves the lock behind,
remove it by hand — `execute` tells you the path.
- In drive mode, closing the planner chat mid-run kills the executor with it;
prefer watch mode for very long tasks.
- Repos initialized before drive mode existed have the old protocol text:
between tasks, copy everything above `## Current Task` from
`templates/HANDOFF.md` over the same region of the repo's HANDOFF.md.
- One task in flight at a time — that's the protocol, not a bug.
- Status parsing expects the template's `**Status:** …` line; if an agent
mangles the heading structure, `handoff status` reports `UNKNOWN` and
`execute` refuses to run.



## Smoke test result (2026-07-15)

Verified end-to-end against a throwaway repo: `handoff init`, a trivial
planned task ("create hello.txt containing 'hello'") set to
`READY FOR EXECUTION`, then `handoff execute`. The executor created the file
(byte-exact `hello\n`), filled in Execution Notes, and set `READY FOR QA`,
in 31s. `git status` in the target repo stayed clean of handoff files,
confirming the `.git/info/exclude` entries.

One real failure was found and fixed during the test: the first run died with
`Failed to authenticate. API Error: 401 Invalid bearer token` because it was
launched from inside another Claude Code session, whose exported
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` overrode the executor CLI's stored
login. `handoff execute` now strips all inherited `ANTHROPIC_*`/`CLAUDE*` env
vars before launching the executor, so it always runs on its own stored
account — this also prevents the planner's credentials from ever leaking into
an executor run.

## Troubleshooting

- `claude: command not found` — set `HANDOFF_CLAUDE_BIN=/Users/CHEN_Dechao/.local/bin/claude`
or fix PATH.
- Executor exits immediately with an auth error — the CLI got logged out of
the executor account; run `claude /login` in a terminal.
- `401 Invalid bearer token` on old versions of this script meant an
`ANTHROPIC_AUTH_TOKEN` in the calling environment was overriding the stored
login; `handoff execute` now strips those vars itself. Note this also means
you cannot authenticate the executor via env vars — the stored login is the
only supported path, by design.
- Executor "did nothing" — read the log in `.handoff-logs/`. Most often the
plan required a command outside the allowlist; either the plan is wrong or
the allowlist needs a deliberate, human-made addition.

