# handoff-cli reference

Command catalog for the Planner skill. `<repo>` defaults to the current
directory; quote it. Every `--json` command prints one JSON object on stdout
with `"schema": "urn:handoff-automation:cli-output:v1"` and a `"kind"`. On
failure the exit code is nonzero and the object has an `"error"` string. No
output contains tokens or credential contents.

## Setup

| Command | Purpose |
| --- | --- |
| `handoff skill install "<repo>" --host <cursor\|codex\|claude>` | Project copy of this skill (git-excluded). `--user` (no repo) installs for every repo on this machine. |
| `handoff skill status "<repo>" --json` | Where the skill is installed (`kind: skill-status`). |
| `handoff init "<repo>" --transport a2a --executor <p> --model "<id>" --mode <drive\|watch> [--reasoning-effort <e>] [--planner <host>] [--port <n>]` | Managed A2A setup. Re-running keeps everything; `--mode` alone changes only the run mode. |
| `handoff init "<repo>" --transport legacy [--planner <host>]` | Legacy direct Claude (no `--mode`). |
| `handoff models "<repo>" --provider <p> --json` | Model IDs the installed CLI reports (`kind: models`). |
| `handoff server start\|status\|stop "<repo>" [--port <n>] [--json]` | The managed local Executor service. |

`--reasoning-effort` is codex only. Cursor model IDs carry their own effort
(for example a `-high` variant). Claude has no model listing: pass an
explicit ID, which is recorded as unverified.

## Workflow

| Command | Purpose |
| --- | --- |
| `handoff status "<repo>" [--json]` | Markdown status, execution, turn, mode, watcher, Executor, next action (`kind: status`). A2A repos with a workflow include `preflight`. |
| `handoff preflight "<repo>" [--json]` | Every execute blocker and its fix (`kind: preflight`). Exit 0 when ready, 1 when blocked. Legacy repos refuse: preflight needs managed A2A. |
| `handoff approve "<repo>"` | Record the human's chat approval of the current plan. |
| `handoff execute "<repo>"` | Dispatch one execution and wait (drive mode; also clears a reviewed dispatch hold). A wait timeout (exit 2, "only this command stopped waiting") means the run is still going; re-run a plain `handoff resume "<repo>"` to reattach. |
| `handoff watch "<repo>"` | Poll and dispatch (watch mode; refuses in drive mode). The human keeps it running. |
| `handoff resume "<repo>"` / `handoff cancel "<repo>"` | Reconnect to / cancel an outstanding run. |
| `handoff runs "<repo>"` | Past runs from manifests. |
| `handoff archive "<repo>" [--superseded]` | Move the finished task to HANDOFF-ARCHIVE.md; `--superseded` closes an exhausted workflow. Refuses a damaged heading structure or a plan that changed since approval. |
| `handoff qa "<repo>" --status <APPROVED\|CHANGES REQUESTED> --file <path> [--append] [--json]` | A2A only (`kind: qa`). Replaces QA Feedback and sets Status. `--append` on APPROVED or CHANGES REQUESTED adds a note and leaves Status unchanged. Refuses while a run is outstanding, on a bad structure, or if Current Task would change. |
| `handoff template refresh "<repo>" [--json]` | Replace the HANDOFF.md preamble from the installed template (`kind: template_refresh`). Everything from `## Current Task` on stays byte-for-byte. Prints "already current" when nothing differs. Refuses, without writing, when the heading is missing or duplicated or an A2A run is outstanding. |

## Executor and run mode

| Command | Purpose |
| --- | --- |
| `handoff model "<repo>" --json` | Selected / active / pending Executor (`kind: model`). |
| `handoff model "<repo>" --provider <p> --model "<id>" [--reasoning-effort <e>] [--after-current] [--json]` | Switch now, or after the current run (`kind: model-change`). |
| `handoff model "<repo>" --cancel-pending` | Drop a queued `--after-current` change. |
| `handoff mode "<repo>" [drive\|watch] [--json]` | Show or set the run mode (`kind: mode`). Managed A2A only. |

## JSON fields the skill reads

- `status`: `transport` (`legacy`|`a2a`), `managed` (bool), `status`
  (Markdown), `turn`, `execution` (`IDLE`, `WORKING`, `SUBMITTED`,
  `UNRESOLVED`, `UNKNOWN`, `RECOVERY`, `COMPLETED`, `FAILED`, `CANCELED`; `null` for
  legacy), `probe` (`not_permitted` when a status query was sandbox-denied, otherwise
  null), `mode` (`drive`|`watch`|`null`), `watcher` (`running`, `stale`,
  `pid`, `started_at`), `workflow` (`workflow_id`, `rounds_used`,
  `max_rounds`, `dispatch_hold`, `last_outcome`,
  `plan_changed_since_approval`), `executor` (as `model`), `run`, `reason`,
  `next`, `preflight` (`ready`, `branch.expected`, `branch.actual`,
  `branch.exists`, `baseline_clean`, `dirty_paths`, `blockers[]` with
  `code`, `message`, `fix`; `null` with no approved workflow or on legacy).
- `models`: `provider`, `models_listed`, `models` (`id`, `name`), `note`,
  `reasoning_effort_supported`, `login_command`. `models_listed: false`
  only means the CLI cannot list models (always for Claude: ask for an
  explicit ID); it says nothing about login. An `error` object has
  `error_kind` (`network`, `auth`, or `other`). `login_command` is present
  only for `auth`. `network` is not a login problem.
- `model`: `selected` (`provider`, `model`, `reasoning_effort`,
  `generation`, `validation`), `active` (`state`:
  `verified`|`unverified`|`stopped`|`unknown`), `current_run`, `pending`.
  `unknown` means the service probe was not permitted.
- `server-status`: `service` (`running`|`stopped`|`unknown`), `state`
  (`verified`|`running`|`stopped`|`unknown`), `probe` (`ok`|`not_permitted`),
  `verified`, `endpoint`, `selected`, `active`, `pid`, `notes`,
  `last_failure`. `unknown` / `not_permitted` means the probe was blocked;
  the process record is kept.
- `mode`: `mode`, `transport`, `managed`, `changed`, `previous`, `watcher`
  (managed) or `hint` (not managed).
- `skill-status`: `locations[]` with `host`, `scope` (`project`|`user`),
  `path`, `state`, `detail`, `install_command`.

## Skill copy states

`absent`; `current`; `outdated` (an unmodified older copy; `skill install` or
re-init upgrades it); `elsewhere` (another folder in that host's skill root
whose `SKILL.md` names `handoff-cli`; `found_path` and `current_content`;
never modified); `foreign` (user-modified, unrelated, symlinked, or
tracked; never overwritten, so tell the human to keep it or move it aside);
`unsupported` (a `--user` location that cannot be verified, for example
Claude with a relocated `CLAUDE_CONFIG_DIR`). `elsewhere` counts as available.

## Environment

`HANDOFF_CLAUDE_BIN`, `HANDOFF_CODEX_BIN`, `HANDOFF_CURSOR_BIN` (provider
CLIs), `HANDOFF_MODEL` (legacy model), `HANDOFF_POLL_INTERVAL` (watch
seconds), `HANDOFF_A2A_BIN` (A2A runtime). Preserve whatever is set.
