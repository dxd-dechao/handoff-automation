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

- **Planner/QA agent** — an agent in the Cursor IDE (e.g. Fable 5). Writes the
  plan, reviews the result. Referred to below as **PLANNER**.
- **Executor agent** — Claude Code in a terminal. Implements the plan. Referred
  to below as **EXECUTOR**.

The two agents share no chat context. Everything they need to know from each
other must be in this file, the git history, or the code itself. The human
relays turns by prompting each agent (e.g. "write a plan to the handoff",
"execute the handoff", "QA the handoff").

---

## Protocol

The workflow is a loop over one task at a time:

1. **Plan** — The human asks PLANNER to plan a task. PLANNER overwrites the
   `Current Task` section below and clears the `Execution Notes` and
   `QA Feedback` sections. It sets `Status: READY FOR EXECUTION`.
2. **Execute** — The human tells EXECUTOR to "execute the handoff". EXECUTOR
   reads `Current Task`, does the work following the Executor Rules, fills in
   `Execution Notes`, and sets `Status: READY FOR QA`.
3. **QA** — The human asks PLANNER to QA. PLANNER reviews the **git diff**
   (not the executor's self-report) against the acceptance criteria, runs
   tests, and writes `QA Feedback`. It sets `Status: APPROVED` or
   `Status: CHANGES REQUESTED`.
4. **Fix loop** — If changes are requested, EXECUTOR addresses only the items
   in `QA Feedback`, appends to `Execution Notes`, and sets
   `Status: READY FOR QA` again. Repeat until approved.
5. **Done** — On `APPROVED`, the human merges/keeps the branch. The next task
   overwrites the working sections; this file keeps no history (git does).

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
- Do not implement the task yourself unless the human asks you to.

### Rules for EXECUTOR (implement)

- **Work on a branch** named in `Current Task` (create it if it doesn't
  exist). Commit as you go with clear messages; do not push or open PRs
  unless the human asks.
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
- `Status` (in Current Task) is the single source of truth for whose turn it
  is: `READY FOR EXECUTION` → EXECUTOR, `READY FOR QA` → PLANNER,
  `CHANGES REQUESTED` → EXECUTOR, `APPROVED` → human.
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
