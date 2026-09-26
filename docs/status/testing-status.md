# Testing status and known limits

Current evidence, 2026-09-26:

- **250 Python tests and 38 legacy smoke checks** pass
  ([A14 report](../qa/qa-a14-unreadable-skill-roots.md)). The 141 tests
  not marked `server` also pass inside a sandboxed Planner shell.
- A live **Claude Code Planner** (auto mode, sandboxed shell) drove a live
  **Cursor Executor** (`grok-4.7-high-fast`) through tasks A8–A14 in drive
  mode. A8 and A12 each had a CHANGES REQUESTED round that the Executor
  fixed and the Planner approved. A11–A14 QA was written with `handoff qa`.
- A live **Cursor CLI Planner** ran the setup interview through to a DRAFT
  ([A7](../qa/qa-a7-skill-first-setup.md)). Cursor Grok and Composer
  Executors and model switches were checked live
  ([A6](../qa/qa-a6-cli-usability.md)).

Known limits:

- Not yet checked live:
  - a Codex Planner host;
  - skill discovery in the Cursor Editor;
  - queued `--after-current` switches.
- No cross-provider correction has succeeded live yet. In A6, a Codex
  correction round edited Planner-owned QA text, and its delivery was
  rightly rejected. Earlier Claude–Codex checks are
  [recorded separately](../history/a2a-replacement-results.md).
- Credential filtering is not OS-level account isolation. User-level Cursor
  rules and skills can reach the Executor.
- One local checkout and one active task at a time. Third-party A2A servers
  are untested.
- None of this measures coding quality, speed, or cost.
