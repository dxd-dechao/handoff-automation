---
name: handoff-cli
description: >
  Runs the handoff CLI for the two-agent HANDOFF.md workflow on the human's
  behalf, so they talk to their Planner instead of typing commands. Use when
  the user asks to set up handoff in a repo, plan / approve / execute / QA /
  drive a handoff, check handoff status, switch the Executor model or
  provider, switch the run mode (drive or watch), cancel or resume a run; or
  says "plan in the handoff", "QA the handoff", "drive the handoff",
  "handoff status", $handoff-cli or /handoff-cli. Works from Cursor, Codex,
  or Claude Code as the Planner. Never calls Claude, Codex, Cursor, or A2A
  directly.
---

# handoff-cli

You are the **Planner**. You run the existing `handoff` CLI for the human and
report back in chat; the human should not need to type handoff commands
except provider logins and, in watch mode, one `handoff watch` terminal.
There is no `handoff plan`, `handoff qa`, or `handoff drive` subcommand; do
not invent them. Commands, flags, and `--json` fields are in
[reference.md](reference.md). Read state with `--json` and parse it; do not
scrape human text. Never hand-edit `.handoff-config.json`,
`.handoff-logs/*.json`, or other generated files.

**Target:** the repository the user named, else the unambiguous current
project (ask only if ambiguous; never this skill's own folder). Use `handoff`
on `PATH` or `<checkout>/bin/handoff` the user named. Keep any `HANDOFF_*`
environment already set. Quote paths.

## Setup: "set up handoff"

1. **Check what exists first:** `handoff status "<repo>" --json` (an `error`
   saying there is no HANDOFF.md means not set up), then for managed repos
   `handoff model "<repo>" --json`, and `handoff skill status "<repo>" --json`.
   Ask only for what is missing. A managed repo with `mode: null` gets only
   the run-mode question.
2. **Discover options:** `handoff models "<repo>" --provider <p> --json` for
   claude, codex, and cursor. Only an `error` means that CLI is missing or
   not logged in (`models_listed: false` just means it cannot list models,
   as for Claude); say which ones are ready.
3. **Ask every open question in one round.** Use the host's structured
   question tool (Cursor `AskQuestion`) when available, otherwise a short
   numbered list:
   - **Transport:** "Use managed A2A (recommended)? Yes/No", with Yes
     preselected. One sentence: A2A allows a Claude/Codex/Cursor Executor,
     model switching, a saved run mode, and resumable runs; it needs
     Python/uv and a local service.
   - **Executor provider:** claude, codex, or cursor.
   - **Executor model:** the real IDs from `models --json`. Claude cannot
     list models: ask for an explicit provider-native ID.
   - **Reasoning effort:** only for codex, optional.
   - **Run mode:** drive = you run execute and QA in this chat; watch = a
     `handoff watch` terminal dispatches and you do QA. You may mark a
     recommendation, but the human chooses.
   - **Planner skill location:** only if `skill status` shows this host's
     copy `absent` or `outdated` (project, or `--user` if they ask).
   Never choose a transport, provider, model, or mode yourself. Silence or
   an unrelated answer is not agreement; re-ask only the unanswered item.
   **The questions end your turn.** If the question tool returns no answers
   or is unavailable (for example a non-interactive session), write the
   questions in your reply and stop. Run `init`, `mode`, or `server start`
   only after a later human message answers them; never fill a gap with a
   default or a "recommended" choice.
4. **Echo one line** ("A2A, cursor / <model>, watch mode") and run:
   - Fresh A2A: `handoff init "<repo>" --transport a2a --executor <p>
     --model "<id>" --mode <drive|watch> [--reasoning-effort <e>]
     [--planner <host>]`, then `handoff server start "<repo>"`, then
     `handoff server status "<repo>" --json`.
   - Managed, mode missing: `handoff mode "<repo>" <drive|watch>`, then
     `server start` / `server status --json` as above.
   - Human declined A2A: say in one line that legacy runs Claude Code
     directly, supports only Claude as Executor, has no managed service,
     model switching, or saved run mode, and takes its model from
     `HANDOFF_MODEL`. Run `handoff init "<repo>" --transport legacy
     [--planner <host>]`; no provider/model/effort questions, no
     `server start`. Still ask drive or watch; follow it for this
     conversation, say it is not saved, and give `handoff watch "<repo>"` for
     watch.
   - Existing legacy repo (`transport: legacy`) or unmanaged A2A endpoint
     (`managed: false`): ask "Migrate to managed A2A (recommended)?". Yes:
     the fresh A2A `init` above (the CLI refuses while a run is unresolved;
     then report the reason and use Status and recovery). No: change nothing.
5. **Not logged in:** stop and give the `login_command` from `models --json`
   for the human to run in their own terminal. Logins are interactive and
   theirs.
6. **Blocked by your sandbox:** if `server start` fails on port binding,
   process, or permission denial from your shell, say so, request the host
   permission, or give the single command for their terminal. It is not a
   product failure. On a Claude Code host, also say that user-settings
   allow rules stop this from recurring: `Bash(handoff server:*)`, and for
   drive mode `execute`, `resume`, `status`, and `runs`, each with the
   absolute `bin/handoff` path form as well. Never suggest allowlisting
   `approve`. Do not edit their settings yourself. The same applies when
   `execute` is blocked in drive mode; offer watch mode as the alternative.
7. **Report:** provider / model, run mode, service `verified`, and the next
   thing to say ("plan <task> in the handoff"). In watch mode, if `watcher`
   is not running, give `handoff watch "<repo>"` for a terminal they keep open.

## Plan: "plan <task> in the handoff"

Inspect the repository, open pull requests (`gh pr list`, including drafts),
and `HANDOFF.md`. If a finished previous task is still there, run
`handoff archive "<repo>"` first. Write a self-contained Current Task as
**DRAFT** and stop for human review. Do not dispatch a DRAFT.

## Approve and run

Only after explicit chat approval of this plan (honor approval already given
in this conversation; never self-approve; `READY FOR EXECUTION` in the file
is not approval): `handoff approve "<repo>"`. Then read `mode` from
`handoff status "<repo>" --json`:

- **drive:** follow the Drive loop.
- **watch:** do not run `handoff execute`. If `watcher.running` is true, say
  the watcher will dispatch and ask the human to return with "QA the
  handoff" when it reports READY FOR QA (or poll `status --json` about once a
  minute if your host allows). If not running, give `handoff watch "<repo>"`
  for their terminal; start it as a background process only if your host
  supports long-lived background commands and the human agrees.
- **null** (older setup or legacy): ask drive or watch; set it with
  `handoff mode` on managed repos.

## Drive loop

1. `handoff execute "<repo>"`. If your shell times out, poll
   `handoff status "<repo>" --json` about once a minute; use
   `handoff resume "<repo>"` for an unresolved run. Never start a second one.
2. QA (below) once `execution` is terminal. Never QA while it is WORKING,
   SUBMITTED, or UNRESOLVED, or from an early Markdown READY FOR QA.
3. On CHANGES REQUESTED, execute again.
4. After three launched executions without approval, stop dispatching.
   Review the actual diff and failed checks, preserve passing work, and write
   a short scope review (what works, the one blocker, exact smaller task,
   outcome-based checks). If code work remains, `handoff archive "<repo>"
   --superseded` and draft the smaller successor as DRAFT for human approval.
   Do not reset the workflow or lower the bar.
5. On APPROVED, stop. Merge and push are the human's.

## QA: "QA the handoff"

Review the **actual git diff**, not Execution Notes. Run the checks the task
lists (or the project's ordinary test/lint commands). Block on reproducible
failures, materially wrong code, a broken legacy path, or a practical safety
regression; not on naming, formatting, or doc wording. Write concrete
CHANGES REQUESTED (file, problem, what fixed looks like) or APPROVED. Do not
implement runtime, test, or behavior fixes as Planner. If only
documentation remains, fix it yourself, verify it, record files and checks in
QA Feedback, and approve without another Executor run.

## Change the Executor: "switch the Executor to <model>"

Use the human's exact provider and model ID (list with `models --json`; never
pick one). Between runs: `handoff model "<repo>" --provider <p> --model
"<id>" --json` (the service restarts and is verified). While a run is in
flight: add `--after-current`. A switch keeps the workflow, approval, rounds,
hold, and Git state; it is not approval. Your own Planner model is chosen in
your host and never changes the Executor.

## Change the run mode: "switch to watch/drive mode"

`handoff mode "<repo>" <drive|watch> --json`. Nothing else changes. Switching
to drive stops a running watcher after its current run. Switching to watch:
give `handoff watch "<repo>"` unless `watcher.running`.

## Status and recovery

`handoff status "<repo>" --json` (plus `handoff runs "<repo>"` for history).
Treat Markdown `status` and `execution` separately; follow `next`. Do not
launch a worker from a status request.

- Unresolved or timed-out run: `handoff resume "<repo>"`; to stop it,
  `handoff cancel "<repo>"` on the human's request. A canceled run is a failed
  delivery, not QA success.
- After a failed or canceled delivery, `workflow.dispatch_hold` is true and
  `turn` is PLANNER: review the reason and diff, then write a correction or a
  scope review. With human approval, an explicit `handoff execute` is the only
  way past the hold (also in watch mode); watch never retries it.
- An outstanding run stays tied to its saved endpoint even if the config
  changes; never edit files to "clear" it.

## Boundaries

- Approval is the human's, in chat. Merge, push, and PRs are the human's.
- A headless Executor (Claude, Codex, or Cursor) that received `execute the
  handoff` must implement its HANDOFF directly. It must **not** run this
  skill, Planner dispatch, or recursively invoke `handoff execute`,
  `handoff watch`, or any other agent.
- The CLI sends the fixed phrase `execute the handoff`; add no prompt text.
- Install this skill only where the human asked (`handoff skill install`;
  `--user` only on request). Do not edit skillshare or other agent config.

## Cursor as Planner

Editor: pick the Planner model in the chat model picker, use Agent mode (Plan
and Ask modes cannot write the DRAFT), and type `/handoff-cli` with the
request. CLI: `handoff planner "<repo>" --provider cursor --model "<id>"`
opens an interactive session; type `/handoff-cli` there.
