# handoff-automation

A local CLI for running a coding task with two agents: a **Planner** that writes
the plan and reviews the result, and an **Executor** that implements it.

Use Claude Code, Codex, or Cursor CLI as the Executor. Choose the Planner's
model independently in its own host. The agents coordinate through
`HANDOFF.md`, with Git changes and tests providing the evidence for review.
You approve the plan and decide when to merge.

After a one-time bootstrap you talk to your Planner agent (Cursor, Codex, or
Claude Code) and it runs the `handoff` CLI for you through the `handoff-cli`
skill: it asks which Executor and model you want and whether it should drive
the loop or leave dispatch to a watcher.

[Quick start](#quick-start) · [Workflow](#workflow) ·
[Model switching](#change-the-executor-model) · [Agent guide](#for-agents) ·
[Development](#development)

## What it does

- Lets your Planner agent set up and run the workflow through the
  `handoff-cli` skill. You answer its questions and approve plans in chat.
- Generates configuration, a local service token, and workflow files. No
  JSON files to create by hand.
- Runs Claude Code, Codex, or Cursor through a managed local A2A service.
  A2A is the protocol the CLI uses to submit and track Executor work.
- Starts each Executor run with fresh context and the current handoff.
- Switches Executor providers or models between runs, or queues a change
  while a run is active.
- Tracks approval, execution rounds, logs, and recovery across a workflow.
- Records a run mode: **drive** (the Planner runs execution and QA itself)
  or **watch** (a `handoff watch` terminal dispatches; the Planner does QA).

This is a local workflow for a Git checkout. The Planner does the planning
and QA; the CLI manages execution. The original direct-Claude mode remains
available under [Legacy mode](#legacy-mode).

## Requirements

- Git, Bash, and `jq` 1.7 or later.
- Python 3.11 or later and `uv` for managed A2A.
- The CLI for each Executor provider you want to use, installed and
  authenticated.
- A Planner agent that can read and edit your repository and run commands.

The documented live checks ran on macOS. Watch notifications use macOS
notifications when available; other platforms have not been validated here.

## Quick start

### 1. Install from source

```sh
git clone https://github.com/dxd-dechao/handoff-automation.git
cd handoff-automation
uv sync
export PATH="$PWD/bin:$PATH"
```

For future terminals, add this checkout's absolute `bin` path to your shell
configuration. The checkout contains the tool; `/path/to/project` below is
the Git repository where you want an Executor to work.

### 2. Log in to your chosen Executor provider

Use the login command for the provider you plan to run:

| Executor provider | CLI | Login |
| --- | --- | --- |
| Claude Code | `claude` | `claude /login` |
| Codex | `codex` | `codex login` |
| Cursor | `cursor-agent` | `cursor-agent login` |

Handoff uses the provider's stored login. You normally authenticate once,
then again if that login expires or is revoked. Logins are interactive, so
they stay yours; the Planner tells you which one to run if one is missing.

### 3. Make the skill available to your Planner

```sh
handoff skill install "/path/to/project" --host cursor    # or --host codex, --host claude
```

This copies the `handoff-cli` skill into the project's skill folder for that
host (`.cursor/skills`, `.agents/skills`, or `.claude/skills`), git-excluded.
Add `--user` (without a project path) to install it for every repository on
this machine instead. Re-running upgrades an unmodified older copy and never
overwrites a skill you changed.

### 4. Ask your Planner to set up handoff

In your Planner chat (Cursor Agent mode, Codex, or Claude Code):

> /handoff-cli set up handoff in this repo

The skill checks what already exists, then asks in one round:

- **Use managed A2A (recommended)?** It is preselected, and you confirm it.
  A2A allows a Claude, Codex, or Cursor Executor, model switching, a saved
  run mode, and resumable runs through a local service. Declining selects
  [legacy mode](#legacy-mode) and the skill explains its limits.
- **Executor provider and model**, from the IDs your installed CLI reports
  (Claude needs an explicit model ID). For Codex, an optional reasoning
  effort.
- **Run mode:** drive or watch (see step 5).

It repeats your choices, runs setup and starts the local service, and
reports the Executor, the mode, and the service state. It never picks a
provider, model, or mode for you.

Some Planner hosts block `handoff server start` because it launches a
background service that listens on a local port. In drive mode they may also
block `handoff execute`. If yours does, the Planner gives you the command to
run in your own terminal. To allow it from then on,
see [When the Planner's host blocks `server start`](docs/cli-setup-and-models.md#when-the-planners-host-blocks-server-start).

### 5. Watch mode only: keep a watcher running

```sh
handoff watch "/path/to/project"
```

Run this yourself in a separate terminal and leave it open; the Planner
does not start it. The watcher does not read your chat. It checks the
handoff state and starts one Executor run each time the Executor has work:
after you approve a plan, and after each `CHANGES REQUESTED`. The Executor
only ever runs the plan you approved; if the plan changes afterwards, it
waits for re-approval.

Both modes share setup, planning, approval, and QA in the Planner chat.
They differ after you approve:

| | Drive | Watch |
| --- | --- | --- |
| Starts each Executor run | The Planner, in the chat | The `handoff watch` terminal |
| Planner chat during a run | Waits for the run to finish | Free to use or close |
| QA after a run | Starts automatically | You ask: "QA the handoff" (the watcher notifies you when the run ends) |
| Extra terminal | None (`handoff watch` refuses to start) | One per repository |

Choose drive for approve-and-wait. Choose watch for long runs, or to keep
the Planner chat free.

That is every command you type. Manual and scripted setup, including the
flags the skill uses, is in the [CLI guide](docs/cli-setup-and-models.md).

## Workflow

Talk to the Planner; it runs the CLI.

| You say | What happens |
| --- | --- |
| "Plan <task> in the handoff" | The Planner inspects the project, writes a self-contained `DRAFT` in `HANDOFF.md`, and stops for your review. |
| "Approved" (after reviewing) | The Planner records your approval. **Drive:** it runs the Executor, waits, and performs QA. **Watch:** your watcher dispatches; the Planner tells you when to ask for QA. |
| "QA the handoff" | The Planner reviews the actual diff and runs the acceptance checks, then writes `CHANGES REQUESTED` or `APPROVED`. |
| "Handoff status" | Markdown status, execution state, whose turn it is, and the run mode. |
| "Switch the Executor to <model>" | Changes the Executor now, or after the current run if one is active. |
| "Switch to watch mode" / "drive mode" | Changes the saved run mode; nothing else changes. |
| "Cancel the run" / "Resume the run" | Stops or reconnects to an outstanding execution. |

`CHANGES REQUESTED` sends an eligible correction back to the Executor.
`APPROVED` leaves the merge decision with you. The Planner never approves its
own plan, and a new plan always waits for you in `DRAFT`.

Each workflow has a bounded execution budget (three runs by default).
If the budget is exhausted, the Planner reviews the remaining scope and
proposes a smaller successor task for your approval. A model change does not
reset that budget.

## Change the Executor model

Ask the Planner, for example: "switch the Executor to Codex `<model-id>`".
It lists the IDs your installed CLI reports and uses the exact ID you pick.

- Between executions, the switch happens immediately and the local service
  restarts on the new model.
- While an execution is active, the current run finishes on its original
  model and the change applies before the next worker starts. A running
  watcher can stay open.

Switching preserves the plan approval, history, round count, and Git state.
If the approved plan explicitly requires a particular Executor, changing it
requires revising and approving that requirement. The Planner's own model is
chosen in its host and never changes the Executor.

Cursor models, including Grok when available to your account, are selected
by the IDs `cursor-agent models` reports for your account; the Cursor
Editor's model picker does not establish CLI access. Codex discovery reports
the installed CLI's catalog; Claude has no model enumeration in this
integration. See the [model guide](docs/cli-setup-and-models.md#2-see-which-models-exist)
for the commands and failure handling.

## For agents

Reading this repository does not activate the handoff protocol. Use it when
the human requests a handoff workflow. The full instructions live in
[`skills/handoff-cli/SKILL.md`](skills/handoff-cli/SKILL.md) and the
[`HANDOFF.md` template](templates/HANDOFF.md).

**Planner / QA agent**

- Resolve the user's target repository and inspect its current status.
- Put scope, files, acceptance checks, and all Executor instructions in
  `HANDOFF.md`. The Executor has no access to your chat context.
- Keep a new plan as `DRAFT` until the human approves it. Record existing
  approval with `handoff approve`; changing a Markdown status alone does
  not create the A2A approval receipt.
- Use the public CLI for setup, selection, execution, and recovery, and
  read state with `--json`. Ask for the transport, provider, model, and run
  mode; never choose them yourself. Use explicit setup flags in
  non-interactive shells.
- Wait for execution to finish and reconcile before QA. Review Git changes
  and test results independently of the Executor's report.
- Respect the execution budget and the human's merge/publish decision.

**Executor agent**

- Implement the assigned handoff directly, using the fixed
  `execute the handoff` invocation.
- Follow its scope and acceptance checks; write results in Execution Notes.
  Preserve the Planner-owned plan and QA sections.
- Do not launch another Executor, invoke the Planner skill, or recursively
  run `handoff execute` / `handoff watch`.

Planning, QA, and driving are agent responsibilities. There are no
`handoff plan`, `handoff qa`, or `handoff drive` commands.

## Recovery and local files

Ask the Planner for "handoff status". If an execution is outstanding, it can
resume (reconnect) or cancel it. A client timeout does not cancel the worker.
After a failed or canceled run the Planner reviews it before dispatching
again; a watcher never retries a held failure.

Managed setup keeps these files local and Git-excluded:

| Path | Purpose |
| --- | --- |
| `HANDOFF.md` / `HANDOFF-ARCHIVE.md` | Current task and archived handoffs |
| `.handoff-config.json` | Client routing |
| `.handoff-logs/server.json` | Managed service and Executor configuration |
| `.handoff-logs/` | Credentials, service state, run logs, and evidence |
| `.cursor/`, `.agents/`, or `.claude/skills/handoff-cli/` | Planner skill copy (`handoff skill install`) |
| `.claude/settings.local.json` | Claude Executor permissions, when applicable |

Let the CLI manage generated configuration (the Planner uses `init`,
`server`, `model`, and `mode`).
Local excludes work with linked Git worktrees. See the
[setup and recovery guide](docs/cli-setup-and-models.md) for migration,
service conflicts, failed model changes, and file details.

## Legacy mode

The original direct-Claude workflow needs no Python runtime or A2A service.
Decline managed A2A in the setup interview and the Planner initializes it
(the command is in [Advanced setup](docs/advanced-setup.md)).

It uses an authenticated `claude` CLI and the generated Claude permission
allowlist. This mode supports only Claude Code, has no model switching or
saved run mode, and takes its model from `HANDOFF_MODEL`. Repositories without
`.handoff-config.json` continue to use this path. For permissions, logging,
and manually configured A2A endpoints, see [Advanced setup](docs/advanced-setup.md).

## Validation and limitations

The [A7 verification report](docs/qa-a7-skill-first-setup.md) records
**192 Python tests and 21 legacy smoke checks passing** on 2026-09-25, and a
live Cursor CLI Planner that ran the setup interview, set up A2A with the
answered provider, model, and watch mode, and stopped at a DRAFT. The
[A6 report](docs/qa-a6-cli-usability.md) covers generated setup, Cursor Grok
and Composer Executors, and immediate model switches.

Known limits of that evidence:

- Cursor Editor skill discovery and invocation remain unverified. Codex and
  Claude Code Planner hosts have installer and fake-worker coverage, not
  live Planner turns.
- The interactive Planner launcher is tested separately; live scripted
  Planner turns used Cursor's print mode.
- In the A6 fixture, a Codex continuation edited Planner-owned QA content.
  Delivery was correctly rejected, so that cross-provider correction did
  not complete successfully. Earlier Claude–Codex replacement checks are
  recorded [separately](docs/a2a-replacement-results.md).
- Queued `--after-current` switching has fake-worker coverage only.
- Provider permissions differ. Credential-environment filtering is not OS
  account isolation; user-level Cursor rules, skills, and MCP configuration
  are not fully isolated from the Executor.
- The implementation targets one local checkout and one active task at a
  time. Third-party A2A server interoperability is not established.

These checks do not establish coding quality, speed, or cost improvements.

## Development

From this tool's checkout:

```sh
uv sync --extra test
bash -n bin/handoff
bash tests/smoke-manifest.sh
uv run --offline --extra test python -m pytest -q
git diff --check
```

The automated suite uses fake provider workers. Server tests need permission
to bind local ports. Live-check scripts require explicit `--confirm-live`
and make paid provider calls against disposable fixtures.

| Location | Contents |
| --- | --- |
| `bin/handoff` | Public Bash CLI |
| `src/handoff_a2a/` | A2A service, adapters, setup, selection, and recovery |
| `skills/handoff-cli/` | Planner skill |
| `templates/` | Handoff protocol and permission templates |
| `tests/` | Automated regression checks |
| `scripts/` | Live acceptance checks |
| `docs/` | Guides, protocol, plans, and recorded QA evidence |

## Documentation

- [CLI setup, Planner hosts, model selection, and recovery](docs/cli-setup-and-models.md)
- [Advanced setup: legacy Claude and manual A2A endpoints](docs/advanced-setup.md)
- [A2A coding-task protocol](docs/a2a-coding-task-v1.md)
- [A7 skill-first setup verification](docs/qa-a7-skill-first-setup.md)
- [A6 CLI and Cursor verification](docs/qa-a6-cli-usability.md)
- [Product history and remaining work](docs/product-status.md)
- [Implementation history](docs/implementation-plan.md)
