# handoff-automation

A local CLI for running a coding task with two agents: a **Planner** that writes
the plan and reviews the result, and an **Executor** that implements it.

Use Claude Code, Codex, or Cursor CLI as the Executor. Choose the Planner's
model independently in its own host. The agents coordinate through
`HANDOFF.md`, with Git changes and tests providing the evidence for review.
You approve the plan and decide when to merge.

[Quick start](#quick-start) · [Workflow](#workflow) ·
[Model switching](#change-the-executor-model) · [Agent guide](#for-agents) ·
[Development](#development)

## What it does

- Generates configuration, a local service token, and workflow files with
  `handoff init`. No JSON files to create by hand.
- Runs Claude Code, Codex, or Cursor through a managed local A2A service.
  A2A is the protocol the CLI uses to submit and track Executor work.
- Starts each Executor run with fresh context and the current handoff.
- Switches Executor providers or models between runs, or queues a change
  while a run is active.
- Tracks approval, execution rounds, logs, and recovery across a workflow.
- Supports a Cursor CLI Planner and an optional project-local Planner skill.

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
handoff --help
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
then again if that login expires or is revoked. The generated local service
token is separate from your provider login.

### 3. Initialize your project and start the service

For guided setup in a terminal:

```sh
handoff init "/path/to/project"
```

Choose `a2a`, your Executor provider and model, and whether to install the
Cursor Planner skill. Then start the service:

```sh
handoff server start "/path/to/project"
handoff server status "/path/to/project"
```

For scripts and agent shells, supply the choices explicitly. For example,
using Cursor as the Executor and installing its Planner skill:

```sh
handoff models "/path/to/project" --provider cursor
handoff init "/path/to/project" --transport a2a \
  --executor cursor --model "<executor-model-id>" --planner cursor
handoff server start "/path/to/project"
```

Replace the model placeholder with an ID reported for your account. Use
`--executor claude` or `--executor codex` for those providers.
`--planner cursor` installs the Planner skill; it does not select the
Planner model.

Setup generates the JSON configuration and token, adds local Git excludes,
and creates `HANDOFF.md` if it is absent. Re-running setup preserves the
existing plan and settings. Neither `init` nor `watch` starts the server.

### 4. Open your Planner

Use your existing Planner host with the
[`handoff-cli` skill](skills/handoff-cli/SKILL.md), or launch Cursor CLI:

```sh
handoff planner "/path/to/project" --provider cursor --model "<planner-model-id>"
```

In that session, invoke `/handoff-cli` and ask it to plan your task in the
handoff. The Planner model can differ from the Executor model.

The Cursor Editor integration uses the same skill in Agent mode. Its
installation is implemented, but Editor discovery and invocation still need
a manual verification; see [Validation and limitations](#validation-and-limitations).
For other Planner hosts, install the skill in a directory that host searches.

## Workflow

1. **Plan.** Ask the Planner: “Plan <task> in the handoff.” It inspects the
   project, writes a self-contained `DRAFT` in `HANDOFF.md`, and asks you
   to review it.
2. **Approve.** After you approve the plan, run
   `handoff approve "/path/to/project"`, or have the Planner record your
   approval with that command.
3. **Execute.** Run `handoff watch "/path/to/project"` in a terminal you
   leave open. For one execution, use `handoff execute "/path/to/project"`.
4. **Review.** When execution is complete and ready for QA, ask the Planner:
   “QA the handoff.” It reviews the actual diff and runs the acceptance
   checks. Watch does not perform QA.
5. **Correct or finish.** `CHANGES REQUESTED` returns an eligible correction
   to the Executor. `APPROVED` leaves the merge decision with you.
   Archive a finished task with `handoff archive "/path/to/project"`
   before planning the next one.

For a Planner-driven loop, ask it to “plan <task> in the handoff and drive
it.” After your approval, the Planner invokes execution and performs QA.
Choose either this mode or a separate Watch process for a repository.

Each workflow has a bounded execution budget (three runs by default).
If the budget is exhausted, review the remaining scope and approve a smaller
successor task. A model change does not reset that budget.

## Change the Executor model

List available models and inspect the current selection:

```sh
handoff models "/path/to/project" --provider cursor
handoff model "/path/to/project"
```

Between executions, select another model or provider:

```sh
handoff model "/path/to/project" --provider codex --model "<model-id>"
```

While an execution is active, queue the change for the next execution:

```sh
handoff model "/path/to/project" --provider cursor --model "<model-id>" --after-current
```

The current execution finishes on its original model. The CLI applies the
queued selection before the next worker starts and manages any service
restart; a running Watch process can stay open. To discard a queued change:

```sh
handoff model "/path/to/project" --cancel-pending
```

Switching preserves the plan approval, history, round count, and Git state.
If the approved plan explicitly requires a particular Executor, changing it
requires revising and approving that requirement.

Cursor models, including Grok when available to your account, are selected
by the IDs returned by `handoff models --provider cursor`. The Cursor
Editor's model picker does not establish CLI access. Codex discovery reports
the installed CLI's catalog; Claude has no model enumeration in this
integration. See the [model guide](docs/cli-setup-and-models.md#2-see-which-models-exist).

## Command reference

The repository argument defaults to the current directory.

| Command | Purpose |
| --- | --- |
| `handoff init <repo>` | Generate setup interactively, or with explicit flags |
| `handoff models <repo> --provider <name>` | Inspect the provider's model catalog |
| `handoff server start\|status\|stop <repo>` | Manage the local A2A service |
| `handoff planner <repo> --provider cursor --model <id>` | Open a Cursor CLI Planner |
| `handoff model <repo>` | Inspect Executor selection, active run, and pending change |
| `handoff status <repo>` | Inspect task status, execution state, and whose turn it is |
| `handoff approve <repo>` | Record human approval for the reviewed plan |
| `handoff execute <repo>` | Dispatch one eligible Executor run |
| `handoff watch <repo>` | Monitor and dispatch eligible Executor turns |
| `handoff resume <repo>` | Reconnect to an outstanding A2A execution |
| `handoff cancel <repo>` | Request cancellation and reconcile its result |
| `handoff runs <repo>` | List execution history |
| `handoff archive <repo>` | Save the finished handoff before the next task |

Run `handoff --help` for setup flags and environment overrides.

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
- Use the public CLI for setup, selection, execution, and recovery.
  Choose the provider/model the human requested and use explicit setup
  flags in non-interactive shells.
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

Start with `handoff status "<repo>"`. If an execution is outstanding,
use `resume` to reconnect or `cancel` to stop it. A client timeout does
not cancel the worker. Review a failed or canceled run before explicitly
dispatching another; Watch does not automatically retry a held failure.

Managed setup keeps these files local and Git-excluded:

| Path | Purpose |
| --- | --- |
| `HANDOFF.md` / `HANDOFF-ARCHIVE.md` | Current task and archived handoffs |
| `.handoff-config.json` | Client routing |
| `.handoff-logs/server.json` | Managed service and Executor configuration |
| `.handoff-logs/` | Credentials, service state, run logs, and evidence |
| `.cursor/skills/handoff-cli/` | Optional Cursor Planner skill |
| `.claude/settings.local.json` | Claude Executor permissions, when applicable |

Use `init`, `server`, and `model` to manage generated configuration.
Local excludes work with linked Git worktrees. See the
[setup and recovery guide](docs/cli-setup-and-models.md) for migration,
service conflicts, failed model changes, and file details.

## Legacy mode

The original direct-Claude workflow needs no Python runtime or A2A service:

```sh
handoff init "/path/to/project" --transport legacy
```

It uses an authenticated `claude` CLI and the generated Claude permission
allowlist. This mode supports only Claude Code. Repositories without
`.handoff-config.json` continue to use this path. For permissions, logging,
and manually configured A2A endpoints, see [Advanced setup](docs/advanced-setup.md).

## Validation and limitations

The [A6 verification report](docs/qa-a6-cli-usability.md) records
**157 Python tests and 17 legacy smoke checks passing** on 2026-09-24.
Live checks exercised generated setup, a Cursor CLI Planner, Cursor Grok
and Composer Executors, and immediate model switches.

Known limits of that evidence:

- Cursor Editor skill discovery and invocation remain unverified.
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
- [A6 CLI and Cursor verification](docs/qa-a6-cli-usability.md)
- [Product history and remaining work](docs/product-status.md)
- [Implementation history](docs/implementation-plan.md)
