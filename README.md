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

4. **Install the executor permission allowlist** (recommended). JSON can't
   carry comments, so it's documented here instead:

   ```sh
   mkdir -p ~/path/to/repo/.claude
   cp templates/executor-settings.json ~/path/to/repo/.claude/settings.json
   ```

   What it grants and why:

   - **allow:** `Edit`/`Write` (implementing the plan), `git add/commit/status/diff/log`
     (commit-as-you-go on a branch), `pnpm test/lint/build` and
     `npx tsx/vitest/tsc/eslint` (running the acceptance checks itself).
   - **deny:** `git push`, `git merge`, `gh pr create`. Publishing requires
     explicit human authorization written into the task plan — and even then a
     human runs it, or temporarily widens the allowlist for that one task.

   If the repo already has a `.claude/settings.json`, merge the `permissions`
   block instead of overwriting.

## The loop

```
                 human approves plan
                        │
   PLANNER writes plan  ▼
   ┌────────────► READY FOR EXECUTION ─────┐
   │                                       │ handoff watch runs
   │                                       │ `claude -p "execute the handoff"`
   │                                       ▼
   │  CHANGES REQUESTED ◄─── QA ─── READY FOR QA
   │        │                │   (watch notifies; human asks PLANNER to QA)
   │        │ watch runs     │
   │        └── executor ────┘
   │                         │
   │                         ▼
   └──── handoff archive ◄ APPROVED  (watch notifies; human merges)
```

Day-to-day:

```sh
handoff status [repo]    # whose turn is it, what's the task
handoff watch  [repo]    # leave running: auto-executes executor turns,
                         # desktop-notifies on planner/human turns
handoff execute [repo]   # one executor run, manually
handoff archive [repo]   # snapshot the finished task before PLANNER
                         # writes the next plan
```

`watch` polls the `Status:` line (default 30s, `HANDOFF_POLL_INTERVAL` to
change). On `READY FOR EXECUTION` / `CHANGES REQUESTED` it runs the executor;
on `READY FOR QA` / `APPROVED` it posts a macOS notification and keeps
watching. If an executor run fails without changing the status, watch does
**not** retry until the status line changes — fix the problem, then touch the
status (or run `handoff execute` by hand).

Executor output is teed to `.handoff-logs/<timestamp>-execute.log` in the
target repo (git-excluded) for post-hoc auditing.

## Invocation discipline

The executor is always invoked with the fixed phrase
**"execute the handoff"** and nothing else. `bin/handoff` deliberately has no
flag, argument, or env var to add instructions to the prompt. If you want the
executor to know something, write it into HANDOFF.md — that keeps the file
the complete, auditable contract, and keeps every executor run reproducible
from the file alone. The same discipline applies to the planner side: "plan …
in the handoff", "QA the handoff".

## Limitations (intentional)

- **The executor runs headless (`claude -p`), so it cannot answer permission
  prompts.** Anything outside the allowlist simply fails. That is the point:
  the allowlist is the executor's authority boundary, and widening it is a
  human decision, not something an agent can talk its way through.
- `--permission-mode acceptEdits` auto-accepts file edits only; shell commands
  still go through the allowlist.
- `watch` is a poller, not a daemon: it runs in a terminal you leave open, and
  one repo per invocation.
- One task in flight at a time — that's the protocol, not a bug.
- Status parsing expects the template's `**Status:** …` line; if an agent
  mangles the heading structure, `handoff status` reports `UNKNOWN` and
  `execute` refuses to run.

## Smoke test result (2026-07-15)

Verified end-to-end against a throwaway repo: `handoff init`, a trivial
planned task ("create hello.txt containing 'hello'") set to
`READY FOR EXECUTION`, then `handoff execute`. The executor created the file,
filled in Execution Notes, and set `READY FOR QA`.

## Troubleshooting

- `claude: command not found` — set `HANDOFF_CLAUDE_BIN=/Users/CHEN_Dechao/.local/bin/claude`
  or fix PATH.
- Executor exits immediately with an auth error — the CLI got logged out of
  the executor account; run `claude /login` in a terminal.
- Executor "did nothing" — read the log in `.handoff-logs/`. Most often the
  plan required a command outside the allowlist; either the plan is wrong or
  the allowlist needs a deliberate, human-made addition.
