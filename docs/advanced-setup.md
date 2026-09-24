# Advanced setup

For generated setup, model selection, and the normal workflow, start with the
[README](../README.md) and [CLI guide](cli-setup-and-models.md). This reference
covers legacy Claude permissions, logging, and manually configured A2A endpoints.

## Legacy Claude setup

This mode runs Claude Code directly and needs no Python runtime or A2A
service. Install the tool on your PATH, authenticate Claude, and initialize
the target project:

```sh
export PATH="/path/to/handoff-automation/bin:$PATH"
claude /login
handoff init "/path/to/project" --transport legacy
```

The `handoff-cli` skill runs the same `init` when you decline managed A2A in
its setup interview. The Planner runs separately in its own host. Legacy has
no saved run mode (`handoff mode` explains how to migrate); the Planner
follows drive or watch for the conversation, and watch means running
`handoff watch "/path/to/project"` yourself. To use Codex or Cursor as the
Executor, use managed A2A setup instead.

Initialization copies the handoff template only if it is absent and adds
workflow paths to the repository's local exclude file. It uses
`git rev-parse --git-path info/exclude`, including for linked worktrees,
rather than modifying the project's shared `.gitignore`.

### Claude permissions

Initialization merges the Executor permission allowlist into
`.claude/settings.local.json`. To add it to an older initialized project:

```sh
handoff permissions "/path/to/project"
```

Existing entries are preserved and deduplicated; shared
`.claude/settings.json` is untouched. Claude combines permissions from both
files, with deny rules taking precedence.

The generated allowlist permits file edits, local Git inspection and commits,
and a fixed set of test, lint, build, and formatting commands. Package-manager
commands follow the repository's lockfile. It denies `git push`,
`git merge`, and `gh pr create`. Additional project-specific commands require
a deliberate permission change. See the
[reference permission template](../templates/executor-settings.json).

The Executor runs headless with `--permission-mode acceptEdits`. File edits
are accepted, while shell commands still require permission. It cannot answer
an interactive permission prompt; review the run log if a command is refused.
Each launch also passes `--disallowedTools` for the `handoff` and
`handoff-a2a` CLIs and the `handoff-cli` skill, so the Executor cannot
dispatch recursively. That rule is not written to `.claude/settings.local.json`,
because a Claude Code Planner in the same repository reads that file and must
keep running `handoff`.

### Logs and recovery

Legacy executions produce these Git-excluded files in `.handoff-logs/`:

| File | Contents |
| --- | --- |
| `<run_id>-execute.log` | Human-readable result, stderr, and summary |
| `<run_id>-result.json` | Raw Claude JSON output |
| `<run_id>-manifest.json` | Timing, usage, cost, status transition, and commits |

Use `handoff runs "<repo>"` to list manifests. Claude's JSON result is written
at the end of a run; this path does not stream live result text.

Legacy execution is a child of the invoking command. Keep the invoking
Planner session open, or use Watch in a terminal for long tasks. If a crash
leaves `.handoff-logs/execute.lock`, inspect it and confirm the worker has
stopped before removing the stale lock.

For a missing Claude executable, fix PATH or set `HANDOFF_CLAUDE_BIN`.
For an expired login, authenticate with `claude /login` again. Legacy
execution strips inherited provider authentication variables and uses the
CLI's stored login.

## Managed A2A execution and advanced endpoint compatibility

Without `.handoff-config.json`, `handoff` stays on the legacy direct-Claude
path and does not import Python. For normal A2A use, follow the
[README Quick start](../README.md#quick-start):
`handoff init --transport a2a` generates the managed configuration and
`handoff server start` starts its local service. You do not need to create or
populate JSON files yourself.

The remaining details cover the lower-level server and compatibility with
older, manually configured endpoints, which remain supported for
`execute`/`resume`/`status`/`cancel`. The JSON example below is only for that
advanced manual setup, not the recommended managed flow. Managed `server` and
`model` commands do not alter older endpoints; `handoff init "<repo>"
--transport a2a --executor ... --model ...` migrates one explicitly (it refuses
while work is unresolved, keeps the workspace ID and a copy of the old
settings, and leaves external credentials and databases alone).

### Advanced: manually configured endpoint

Manually configured example (placeholders only; never commit secrets):

```json
{
  "transport": "a2a",
  "max_rounds": 3,
  "a2a": {
    "agent_card_url": "http://127.0.0.1:8765/.well-known/agent-card.json",
    "workspace_id": "my-project",
    "credential_file": "/path/to/local-token",
    "request_timeout_s": 30,
    "wait_timeout_s": 180,
    "poll_interval_s": 1
  }
}
```

Relative `credential_file` paths resolve against the target repo. Invalid or
unknown transport fails; it does not fall back to legacy. For a manual
endpoint, configure the provider binary and model in the server config. `HANDOFF_MODEL` applies to legacy only; managed setup also supports
`HANDOFF_CLAUDE_BIN`, `HANDOFF_CODEX_BIN`, and `HANDOFF_CURSOR_BIN`.
Point `HANDOFF_A2A_BIN` at the `handoff-a2a` executable if it is not on
`PATH`. Start the manually configured server yourself:

```sh
uv sync --extra test
uv run handoff-a2a serve --config server.json
handoff approve /path/to/workspace
handoff execute /path/to/workspace
handoff resume /path/to/workspace
handoff cancel /path/to/workspace
```

Server config (loopback host only; generated as `.handoff-logs/server.json`
in managed repos) supplies `host`, `port`, `workspace_id`, `workspace_path`,
`credential_file`, `evidence_dir`, and exactly one Executor adapter:

- `"claude": {"binary": "claude", "model": "..."}` runs `claude -p "execute the
  handoff" --permission-mode acceptEdits --disallowedTools <handoff CLI and
  skill> --output-format json`. Allowed commands come from the target repo's
  `.claude/settings.local.json` (`handoff permissions`).
- `"codex": {"binary": "codex", "model": "...", "reasoning_effort": "medium"}`
  (`reasoning_effort` is optional) runs `codex exec --json
  --ignore-user-config` with `approval_policy="never"` and a `handoff`
  permission profile. That profile extends Codex's `:workspace` sandbox with
  write access to `.git`, so the Executor can commit. Network stays off, so
  push/PR cannot reach a remote. The user's `~/.codex/config.toml` (hooks,
  plugins, default model), user-scope skills, and a project-local
  `.agents/skills/handoff-cli` Planner skill are ignored; the stored Codex
  login is used. HANDOFF.md is git-excluded, so the validated snapshot is
  passed to Codex as `developer_instructions`, alongside the repo's own
  AGENTS.md guidance.
  Inherited `OPENAI_*` / `CODEX_API_KEY` values (and a Cursor Planner shell's
  `CURSOR_*`) are stripped. Codex reports no
  price, so `cost_usd` stays `null`. These sandboxes are not equivalent to
  Claude's permission file (for example, Codex may also write temp
  directories).
- `"cursor": {"binary": "cursor-agent", "model": "..."}` (optional
  `api_key_file`) runs `cursor-agent --print --output-format stream-json
  --model <id> --workspace <repo> --trust --force --sandbox enabled "execute
  the handoff"`. `--force` (required for print-mode edits) is paired with a
  per-run `CURSOR_CONFIG_DIR` whose deny rules block `git push/merge/remote`,
  `gh`, the `handoff` CLI, and nested agents, and whose sandbox gives shell
  commands no network. The stored Cursor login is used; inherited `CURSOR_*`
  and other providers' credentials are stripped. The validated snapshot is
  delivered as a temporary, git-excluded `.cursor/rules/handoff-executor-<execution_id>.mdc`
  (always applied, alongside AGENTS.md and existing rules; removed once the
  worker stops). The model Cursor reports is recorded next to the requested
  one. Cursor reports no price, so `cost_usd` stays `null`.

In a managed repo, `handoff model` replaces the Executor: it restarts the
one owned server on the same port and database with the next
`config_generation`, and the Agent Card advertises `workspace_id` and
`config_generation` so `execute`/`watch` never dispatch to a service that is
not running the selection. With a manually configured endpoint, replacing
the Executor still means running another server and pointing
`a2a.agent_card_url` at it after the previous run is reconciled. Planner
steps and the client do not change. Existing Claude-only server configs keep
working. Optional keys: `state_db` (default `evidence_dir/state.sqlite`),
`caller_id` (default `local-planner`), `execution_timeout_s` (default 3600),
`cancel_grace_s` (default 5). The integrated CLI also requires Agent Card
`workspace_code_fingerprint` and `durable_execution_id_deduplication`. Client
wait timeout does not cancel the worker.

An outstanding run stays authoritative after the config changes. If
`.handoff-config.json` is switched to legacy, pointed at another endpoint, or
removed while a run is outstanding, `status` still shows WAIT/RECOVERY.
`resume`/`cancel` use the run's saved endpoint and credential reference
(default wait limits apply when no A2A config is present). No new execution
starts until the run is reconciled. A failed or canceled delivery does not
keep an Executor-written `READY FOR QA`/`APPROVED`: reconciliation restores the
submitted Status, and `status` routes to Planner review. Its dispatch hold
also survives config changes: `handoff watch` (A2A or legacy) will not
redispatch, and only an explicit `handoff execute` clears the hold. Development helper:

```sh
uv run --extra test python -m pytest -q
uv run handoff-a2a --help
uv run handoff-a2a execute --repo /path/to/workspace \
  --agent-card-url http://127.0.0.1:PORT/.well-known/agent-card.json \
  --credential-file /path/to/token --workspace-id that-workspace
```

Tests must point `claude.binary` / `codex.binary` / `cursor.binary` at a fake
executable, never a paid CLI. The explicit live checks
`scripts/a2a_live_check.py --confirm-live` and
`scripts/a2a_cursor_live_check.py --confirm-live` make real, paid model calls
against disposable fixtures ([A4 results](a2a-replacement-results.md),
[A6 results](qa-a6-cli-usability.md)). If
restart recovery reports `recovery_required`, inspect
`.handoff-logs/execute.lock/owner.json` and `state.sqlite` and remove the lock
only after confirming the worker is stopped.

Profile: [`docs/a2a-coding-task-v1.md`](a2a-coding-task-v1.md).
