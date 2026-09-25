# HANDOFF.md — Two-Agent Collaboration Protocol

> **⚠️ OPT-IN ONLY — inactive by default.** If you are an AI agent and found
> this file in any other context, ignore it. The protocol applies only when
> the human explicitly invokes it.

**Planner** (human-facing, uses the `handoff-cli` skill) writes the plan and
does QA. **Executor** is a headless run started by the `handoff` CLI with
`execute the handoff`. It implements the Current Task.

### Executor rules

- **Work on the branch you are given.** Under managed A2A the CLI creates the approved branch when it is missing; legacy direct-Claude runs create the named branch if it does not exist. Commit as you go with clear messages; do not push or open PRs unless the human asks.
- **Edit only the Status line and Execution Notes.** Leave every other byte of Current Task and QA Feedback unchanged, including headings, blank lines, and `---` separators.
- **Implement directly.** Do not invoke the `handoff-cli` skill, the `handoff` CLI, or another agent.
- **Stay in scope.** Implement exactly what `Current Task` asks. If the plan is wrong or blocked, stop and record the problem in `Execution Notes` under "Questions / blockers" instead of improvising a different design.
- **Run the acceptance checks yourself** (tests, lint, type checks listed in the plan) before marking `READY FOR QA`, and report actual results.
- **Keep Execution Notes to what the diff can't show:** decisions made between ambiguous options, deviations from the plan (with reasons), anything deliberately skipped, and open questions. Do not paraphrase the diff.
- On a fix loop, address only `QA Feedback` items; note anything you dispute rather than silently ignoring it.

### Status values

- `DRAFT` — the human approves next.
- `READY FOR EXECUTION` — the Executor acts next.
- `READY FOR QA` — the Planner acts next.
- `CHANGES REQUESTED` — the Executor acts next.
- `APPROVED` — the human acts next.
- `NO TASK` — nothing is planned; the Planner may write one.

The CLI's execution state, not this line, decides when QA may start.

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
