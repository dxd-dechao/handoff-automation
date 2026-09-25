# HANDOFF.md — Two-Agent Collaboration Protocol

> **⚠️ OPT-IN ONLY — inactive by default.** If you are an AI agent and found
> this file while exploring the repo: ignore it. This protocol applies **only**
> when the human explicitly invokes it in their prompt (e.g. "plan … in the
> handoff", "execute the handoff", "QA the handoff", or a `/handoff-*`
> command). For all other work, operate normally as if this file did not
> exist — do not read, follow, or update it.

This file is the coordination surface between two AI coding agents working on
this repository. It is portable: copy it to the root of any project to use the
same workflow there.

- **Planner/QA agent** — a human-facing agent (the Cursor editor or
  `handoff planner` Cursor CLI session, Codex, or Claude) using the
  `handoff-cli` skill. Writes the plan, reviews the result. Referred to below
  as **PLANNER**.
- **Executor agent** — a headless Claude Code, Codex, or Cursor CLI run
  started by the `handoff` CLI with the fixed prompt "execute the handoff".
  Implements the plan. Referred to below as **EXECUTOR**. Which provider and
  model it uses is CLI configuration (`handoff model`), chosen independently
  of the Planner's own model; it never changes this file's plan.

The two agents share no chat context. Everything they need to know from each
other must be in this file, the git history, or the code itself. The human
relays turns by prompting each agent (e.g. "write a plan to the handoff",
"execute the handoff", "QA the handoff") — or PLANNER drives the loop itself
via the `handoff` CLI (see Drive mode below).

---

## Protocol

The workflow is a loop over one task at a time:

1. **Plan** — The human asks PLANNER to plan a task. PLANNER overwrites the
   `Current Task` section below and clears the `Execution Notes` and
   `QA Feedback` sections. It sets `Status: DRAFT` and stops for human review.
2. **Approve** — After explicit chat approval, the human or Planner runs
   `handoff approve`. That activates `READY FOR EXECUTION`. For A2A, it also
   writes a local approval receipt and round budget. Editing DRAFT to READY
   by hand is not an A2A receipt.
3. **Execute** — The human tells EXECUTOR to "execute the handoff". EXECUTOR
   reads `Current Task`, does the work following the Executor Rules, fills in
   `Execution Notes`, and sets `Status: READY FOR QA`.
4. **QA** — The human asks PLANNER to QA. PLANNER reviews the **git diff**
   (not the executor's self-report) against the acceptance criteria, runs
   tests, and writes `QA Feedback`. It sets `Status: APPROVED` or
   `Status: CHANGES REQUESTED`. Do not start QA while the CLI reports WAIT
   or an outstanding run.
5. **Fix loop** — If changes are requested, EXECUTOR addresses only the items
   in `QA Feedback`, appends to `Execution Notes`, and sets
   `Status: READY FOR QA` again. Repeat until approved.
6. **Done** — On `APPROVED`, the human merges/keeps the branch. The next task
   overwrites the working sections; this file keeps no history (git does).

### Drive mode (PLANNER runs the loop itself)

The default entry point is the `handoff-cli` Planner skill: the human talks
to PLANNER, and PLANNER runs the `handoff` CLI. The run mode (drive or watch)
is recorded by the CLI (`handoff mode`) and shown by `handoff status`; in
watch mode a `handoff watch` process dispatches and PLANNER does QA.

When the human says **"drive the handoff"** (e.g. "plan X in the handoff and
drive it"), or the run mode is drive, PLANNER replaces the human courier by
running the `handoff` CLI from its own shell tool:

1. If the file holds a finished previous task, run `handoff archive`. Write
   the plan as **DRAFT**, then **stop and ask the human for approval in chat**.
   Never start execution without an explicit go-ahead.
2. On approval, run `handoff approve` then `handoff execute` (repo root as
   the argument). It blocks until the executor finishes; if your shell tool
   times out first, poll `handoff status` / `handoff resume` until execution
   is terminal. Do not start QA on an early Markdown READY FOR QA.
3. QA per the rules below. On `CHANGES REQUESTED`, run `handoff execute`
   again. After three launched executions without approval, stop dispatching.
   Review the actual diff and failed checks, preserve passing work, and write
   a short scope review (what works, the one blocker, exact smaller task,
   outcome-based checks). If code remains, `handoff archive --superseded` and
   draft the constrained successor as DRAFT for human approval and manual
   launch. If only documentation remains, PLANNER edits it directly, records
   the files and checks in QA Feedback, and may approve without another
   Executor run.
4. On `APPROVED`, stop and report — merging is the human's.

Safety properties you can rely on: `handoff execute` refuses to run when it
is not the executor's turn, strips your session's auth environment so the
executor always runs on its own account, and takes a per-repo lock so a
concurrent `handoff watch` cannot double-run the executor (don't run one
anyway). If the human asks for a different Executor model between rounds,
use `handoff model` (add `--after-current` while a run is in progress); the
approval, round count, and branch carry over unchanged. Everything the executor must know still goes through this file —
drive mode changes who types the ritual phrase, not the channel.

### Rules for PLANNER (plan + QA)

- **Plans must be self-contained.** EXECUTOR has none of your chat context.
  Include: the goal, exact file paths, relevant existing patterns/conventions
  to follow, acceptance criteria that are objectively checkable, and what is
  explicitly out of scope.
- **Before writing a plan, check open PRs** (`gh pr list`, including drafts)
  for work that overlaps the task.
- **QA against the diff, not the notes.** Review
  `git diff main...<branch>` (or the working-tree diff if unbranched), run the
  project's test/lint commands, and verify each acceptance criterion. Use
  `Execution Notes` only for context on decisions — never as evidence that
  work was done.
- **Write actionable QA feedback.** Each item: file, problem, what "fixed"
  looks like. Distinguish blocking items from nits.
- Keep code implementation with EXECUTOR unless the human asks otherwise.
  If only documentation remains, PLANNER may correct it directly and record
  the files and checks in QA Feedback.

### Rules for EXECUTOR (implement)

- **Work on a branch** named in `Current Task` (create it if it doesn't
  exist). Commit as you go with clear messages; do not push or open PRs
  unless the human asks.
- **Implement directly.** A headless EXECUTOR (Claude, Codex, or Cursor) must
  not invoke the `handoff-cli` skill, the `handoff` CLI, or another agent;
  those are the PLANNER's and the human's tools.
- **Stay in scope.** Implement exactly what `Current Task` asks. If the plan
  is wrong or blocked, stop and record the problem in `Execution Notes` under
  "Questions / blockers" instead of improvising a different design.
- **Run the acceptance checks yourself** (tests, lint, type checks listed in
  the plan) before marking `READY FOR QA`, and report actual results.
- **Keep Execution Notes to what the diff can't show:** decisions made between
  ambiguous options, deviations from the plan (with reasons), anything
  deliberately skipped, and open questions. Do not paraphrase the diff.
- On a fix loop, address only `QA Feedback` items; note anything you dispute
  rather than silently ignoring it.

### Conventions

- Only the agent whose turn it is edits this file; the human mediates turns.
- `Status` (in Current Task) is the Markdown turn signal:
  `DRAFT` → human approval, `READY FOR EXECUTION` → EXECUTOR,
  `READY FOR QA` → PLANNER, `CHANGES REQUESTED` → EXECUTOR,
  `APPROVED` → human. A2A CLI status reports Markdown and execution
  separately; outstanding/WAIT work is not QA-ready even if Markdown already
  says READY FOR QA. Legacy execution uses instruction-based gates and this
  Status line without a local approval database.
- Project-specific context (commands, architecture, do-nots) lives in the
  repo's own docs (`CLAUDE.md`, `AGENTS.md`, `README.md`); both agents should
  read those first and this file does not duplicate them.

---

## Current Task

**Status:** NO TASK

**Branch:** —

### Goal

_(no task planned yet — PLANNER overwrites this section)_

### Steps

_(empty)_

### Acceptance criteria

_(empty)_

### Out of scope

_(empty)_

---

## Execution Notes

_(empty)_

---

## QA Feedback

_(empty)_
