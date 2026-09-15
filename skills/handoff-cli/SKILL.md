---
name: handoff-cli
description: >
  Invokes the existing handoff CLI for the two-agent HANDOFF.md workflow.
  Use when the user asks to plan, execute, QA, drive, or check status of a
  handoff; when they say "plan in the handoff", "execute the handoff",
  "QA the handoff", "drive the handoff", or "handoff status"; and when they
  explicitly select $handoff-cli. Covers handoff execute, status, runs,
  watch, archive, init, permissions, approve, resume, and cancel. Does not
  call Claude, Codex, or A2A directly.
---

# handoff-cli

Instruction-only skill for an **external Planner**. It calls the existing
`handoff` CLI. There is no `handoff plan`, `handoff qa`, or `handoff drive`
subcommand — do not invent them.

These are skill intents, not extra CLI verbs: plan, execute, QA, drive, status.

## Resolve the target

1. Use the repository the user named. If they did not name one, use the
   unambiguous current project. Ask only if the target is ambiguous.
2. Do **not** treat this skill's install directory as the target repo.
3. Do not hardcode a developer's home directory.
4. Find the `handoff` executable on `PATH`, or use an explicit checkout the
   user named (`<checkout>/bin/handoff`). Preserve any `HANDOFF_CLAUDE_BIN`,
   `HANDOFF_MODEL`, `HANDOFF_POLL_INTERVAL`, or `HANDOFF_A2A_BIN` already
   configured in the environment; do not override them unless the user asked.
5. Quote paths that contain spaces.

Read-only inspection: `handoff status "<repo>"` and `handoff runs "<repo>"`.
After a timeout or unresolved execution, inspect `handoff status` / `handoff resume`
before starting QA. Do not start QA from an early Markdown `READY FOR QA`
while execution is still WAIT/WORKING.

## Plan

Inspect the target repository, open pull requests (`gh pr list`, including
drafts), and the current `HANDOFF.md`. Draft a self-contained Current Task.

- Write the task as **DRAFT** and stop for human review.
- Do **not** dispatch an unapproved draft (`handoff execute` or watch).
- If a finished previous task is still in the file, `handoff archive "<repo>"`
  first, then write the new plan.
- After explicit chat approval, run `handoff approve "<repo>"`. Honor approval
  already given in this conversation; do not ask for the same approval again.
  Editing DRAFT to READY FOR EXECUTION by hand is not an A2A approval receipt.

## Execute

Honor approval **already given** in this conversation for the current reviewed
plan. Do not ask for the same approval again.

- `READY FOR EXECUTION` / `CHANGES REQUESTED` in the file is **not** evidence
  of chat approval. Activate an eligible status only after that approval
  exists for this plan (`handoff approve` in A2A mode).
- Then invoke `handoff execute "<repo>"`.
- If execute returns unresolved/timeout, use `handoff status` and
  `handoff resume "<repo>"`. Do not start another execution.
- `handoff cancel "<repo>"` requests cancellation of the outstanding run. A
  canceled run is a failed delivery, not QA success.
- Do not add extra prompt text. The CLI uses the fixed phrase
  `execute the handoff`.

## QA

Inspect **actual git changes**, not Execution Notes. Run the acceptance checks
listed in the current handoff (or the project's ordinary test/lint commands).

Practical bar: block on a reproducible failure in a supported workflow,
materially incorrect code, a broken legacy path, or a practical safety
regression. Do not block on naming, formatting, speculative edges, or exact
documentation wording.

Write concrete `CHANGES REQUESTED` (file, problem, what fixed looks like) or
`APPROVED`. Do **not** implement runtime/test/behavior fixes as Planner unless
the remaining work is documentation-only (see below). Merge/push remain a
human gate.

If only documentation remains, edit those docs directly, verify factual and
runnable accuracy, record the files and checks in QA Feedback, and approve if
functional criteria already pass. Do not spend another Executor run on
documentation cleanup. This does not cover disguising runtime, tests,
permissions, or behavior-changing skill/template edits as documentation-only.

## Drive

Keep the same Planner for execution and QA. After the plan is approved in
chat (starting drive does **not** silently approve a newly generated plan):

1. `handoff approve "<repo>"` then `handoff execute "<repo>"` (or poll
   `handoff status` / `handoff resume` if the shell times out)
2. QA as above. Do not QA while status shows WAIT/WORKING.
3. On `CHANGES REQUESTED`, execute again
4. After three launched executions without approval, stop dispatching. Review
   the actual diff and failed checks, preserve passing work, and write a short
   scope review: what already works, the one important blocker, exact
   files/behavior for a smaller task, and a small set of outcome-based checks.
   If code work remains, `handoff archive "<repo>" --superseded` and draft the
   constrained successor as DRAFT for human approval and manual Executor
   launch. Do not reset the exhausted workflow or silently lower the bar.
5. On `APPROVED`, stop. Merging is the human's

## Status

Use `handoff status "<repo>"` and, when relevant, `handoff runs "<repo>"`.
Treat Markdown status and execution state separately. Do not launch a worker.
Do not announce QA readiness from Markdown alone.

## Boundaries

- Provider selection stays in CLI/backend configuration. This skill never
  calls Claude, Codex, or A2A itself.
- A headless Executor that received `execute the handoff` must implement its
  assigned HANDOFF directly. It must **not** run this skill, Planner dispatch,
  or recursively invoke `handoff execute`.
- Do not install this skill globally or change skillshare/agent configuration
  unless the user asked.

## Install (users)

Keep this folder as the source. Copy or symlink `skills/handoff-cli` into a
Planner skill directory the host actually searches, for example a project
`.cursor/skills/handoff-cli` or a user-level skills directory for
cross-repository use. Official skill discovery guidance:
https://learn.chatgpt.com/docs/build-skills
