## Current Task

**Status:** READY FOR EXECUTION

**Branch:** codex/a7-skill-first-setup

**Task ID:** A7-SKILL-FIRST-SETUP

**Predecessor:** A6-CLI-USABILITY-CURSOR, APPROVED at c9f97b9; archived to HANDOFF-ARCHIVE.md on 2026-09-24. This is new product scope, not an A6 correction.

**Approval:** Planning requested on 2026-09-24. This draft is not execution approval. After explicit human approval, activate it for one manually launched long Executor session, followed by independent QA from the original Planner. Do not launch implementation from this planning task.

### Goal and user requirements

Make the `handoff-cli` Planner skill the default way to use handoff, so a human talks to their Planner agent instead of typing CLI commands. The skill interviews the human for the Executor provider, model, and related options, and for the run mode (drive or watch). It then runs the CLI on their behalf.

The user said: "there are too many CLI commands … can we default to ask AI to use the skill to run the CLIs? the skill ask human for executor information (provider, model and others), and what mode they want (drive or watch)." In a follow-up: "also let the skill defaults A2A, and ask if human agrees in the Setup interview."

Completion means:
1. After a one-time bootstrap (install the tool, log in to the Executor provider, make the skill available to the Planner host), a human can say "set up handoff in this repo" and the agent asks the required questions, runs setup and server start, and reports readiness. The human types no other handoff command.
2. Managed A2A is the skill's default transport. The Setup interview proposes it and asks the human to confirm; declining selects legacy direct-Claude with its limits explained.
3. The chosen run mode (drive or watch) is persisted by the CLI, shown in status, and honored by the skill and by `handoff watch`.
4. Plan, approve, execute, QA, model change, mode change, status, and recovery are all reachable through chat intents that map to existing CLI commands.
5. README, `handoff --help`, and `handoff init` output present the skill-first path as the default; the full command reference remains available for manual/scripted use.

A skill rewrite with no CLI support for run mode, a skill that silently chooses a provider/model, or docs that still lead with a CLI command list do not satisfy the request.

### Verified baseline

- Repository: /Users/CHEN_Dechao/Documents/GitHub/handoff-automation.
- Planning HEAD: 2a5bf25 (Update README.md) on `codex/a2a-executor-mvp`, which contains all A6 commits through c9f97b9. Record the actual execution starting HEAD.
- Uncommitted human work at planning time: modified `README.md` (restructured, ~300 lines changed) and new untracked `docs/guides/advanced-setup.md`. Treat them as the starting documentation content. Do not revert or discard them. Commit them as their own first commit on the new branch ("docs: human README restructure and advanced setup") before your changes, unless they were already committed by the human.
- Open PRs on 2026-09-24: none, including drafts.
- Last recorded regression: 157 Python tests and 17 legacy smoke checks at A6 QA (docs/qa/qa-a6-cli-usability.md). This planning turn did not rerun them.
- Skill: `skills/handoff-cli/SKILL.md` (~180 lines) covers plan/execute/QA/drive/status. It has a "Setup and Executor selection" section that lists flags but no interview flow, and no run-mode concept. `tests/test_a2a_contracts.py` (~line 188) and `tests/test_a2a_cursor_planner.py` pin parts of its text and byte-identity of the installed copy.
- Skill install: `setup.py` `install_planner_skill` / `planner_skill_state` copy the whole `skills/handoff-cli` tree into `<repo>/.cursor/skills/handoff-cli` only (`--planner cursor|none`). State is `absent`, `current` (identical tree digest), or `foreign` (anything else, symlink, or tracked). Consequence: once SKILL.md changes, every A6-installed copy becomes `foreign`, re-init fails with "not overwriting", and `handoff planner` prints a mismatch note. There is no upgrade path.
- `workspace.py` `PLANNER_SKILL_DIR = ".cursor/skills/handoff-cli"`; `is_workflow_path` excludes only that skill location from fingerprints. `setup.py` `PLANNER_EXCLUDE` adds only that path to local git excludes.
- Managed client config (`config.py`, schema `urn:handoff-automation:managed-service:v1`) has `managed.server_config` and `managed.planner.host`. No run-mode field exists.
- `bin/handoff cmd_watch` delegates to `a2a_cli ... watch` for A2A; legacy watch is a Bash loop. Drive vs watch is documented only as advice ("choose either this mode or a separate Watch process"), nothing enforces or records it.
- No command has machine-readable output. `handoff models`, `model`, `status`, and `server status` print human text that the skill must scrape.
- `templates/executor-settings.json` (Claude Executor allowlist) has no deny rule for the `handoff` CLI. The Cursor adapter's per-run config denies `handoff` and nested agents. The Codex adapter overrides user-scope skills (`tests/test_a2a_adapters.py` ~176–187). A project-local `.claude/skills/handoff-cli` would be visible to a headless Claude Executor; its protection today is only the skill's own "Executor must not run this skill" text plus Claude's permission prompts, which headless mode cannot answer.
- Codex discovers project skills in `<repo>/.agents/skills` (verified in A4). Claude Code project-skill location (`.claude/skills/<name>/SKILL.md`) and user-level locations for all three hosts are NOT verified in this repo; check installed CLIs/docs before depending on them.
- No repository AGENTS.md or CLAUDE.md exists. Read them if added before execution.

### Intended user experience

Target human flow (the only terminal commands the human runs):

```sh
git clone https://github.com/dxd-dechao/handoff-automation.git && cd handoff-automation && uv sync
export PATH="$PWD/bin:$PATH"
cursor-agent login            # or: claude /login, codex login — the Executor account
handoff skill install "/path/to/project" --host cursor     # or --host codex|claude; --user for all repos
```

Then, in the Planner chat (Cursor Agent mode, Codex, or Claude Code), with `/handoff-cli` or natural language:

- "Set up handoff" → interview → `handoff init … --mode …` + `handoff server start` → readiness report.
- "Plan <task> in the handoff" → DRAFT → human approves in chat → `handoff approve`, then per mode: drive runs execute+QA loop; watch ensures a watcher is running and waits.
- "QA the handoff", "handoff status", "switch the Executor to <model>", "switch to watch mode", "cancel the run".

Setup interview contract (the skill asks; it never picks a paid option silently):

| Question | Source of options | Notes |
| --- | --- | --- |
| Target repository | current project or user-named path | Confirm only when ambiguous |
| Transport | A2A (proposed default) or legacy | Ask "Use managed A2A (recommended)?" as a yes/no with A2A preselected; see Transport default below |
| Executor provider | claude, codex, cursor | Mention which CLIs are installed/logged in if detectable |
| Executor model | `handoff models <repo> --provider <p> --json` | Show real IDs; Claude has no enumeration → ask for an explicit ID |
| Reasoning effort | only when provider is codex | Optional; skip question for other providers |
| Run mode | drive, watch | Explain in one line each; the skill may mark a recommendation but must ask |
| Planner skill location | host the skill is running in; project or user scope | Only if not already installed where needed |

Ask all questions in one round using the host's structured question tool when available (Cursor `AskQuestion`), otherwise a concise numbered list. Echo a one-line summary of the choices before running commands. If a provider is not logged in, stop and give the human the exact login command to run in their own terminal (logins are interactive and human-owned).

CLI additions TO IMPLEMENT (the skill calls these; humans may too):

```sh
handoff skill install [repo] --host <cursor|codex|claude> [--user]    # bootstrap; idempotent; upgrades unmodified copies
handoff skill status  [repo]                                           # where installed, current/outdated/foreign
handoff init <repo> --transport a2a --executor <p> --model "<id>" --mode <drive|watch> [--planner <cursor|codex|claude|none>]
handoff mode <repo>                     # show run mode
handoff mode <repo> <drive|watch>       # change run mode
handoff models|model|status|server status <repo> ... --json            # stable machine-readable output
```

### Design decisions

Transport default (skill and interactive CLI):
- The skill proposes managed A2A as the default and asks the human to agree in the same question round as the other setup choices. In one sentence, explain that A2A allows a Claude/Codex/Cursor Executor, model switching, a saved run mode, and resumable runs, and that it needs Python/uv plus a local service. Agreement is required. Never treat silence or an unrelated answer as consent; re-ask only the transport question.
- If the human agrees: continue the A2A interview (provider, model, Codex reasoning effort, run mode) and run `handoff init --transport a2a …` then `handoff server start`.
- If the human declines: explain in one line that legacy runs Claude Code directly, supports only Claude as Executor, has no managed service, model switching, or saved run mode, and uses `HANDOFF_MODEL` from the environment for the model. Then run `handoff init "<repo>" --transport legacy` (plus the skill-location choice) and skip the provider/model/effort questions and `server start`. Still ask drive or watch; follow it for this conversation, tell the human it is not saved in legacy, and give the `handoff watch` command when watch is chosen.
- Existing repos: the skill detects the current transport first (`handoff status --json` reports it). A managed A2A repo gets no transport question. A legacy-initialized repo (HANDOFF.md present, no config) or an unmanaged manual A2A endpoint gets "Migrate to managed A2A (recommended)?". On yes, run the explicit migration (`handoff init … --transport a2a --executor … --model …`), which the CLI already refuses while a run is unresolved; on refusal, report the reason and the status/resume/cancel route. On no, leave the repo unchanged.
- `handoff status --json` includes `transport` (`legacy` | `a2a`) and `managed` (bool) so the skill does not parse config files.
- Interactive CLI: the bare `handoff init` TTY transport prompt shows A2A as the default (`[A2A/legacy]`, Enter accepts A2A). Non-interactive behavior is unchanged: flags still decide, bare non-interactive new init still prints the flag hint and exits 2, and a bare re-init of an existing repo stays nondestructive. The Executor provider/model must still be chosen explicitly; defaulting the transport never defaults a paid model.

Run mode:
- Stored in the managed client config as `managed.run_mode` (`"drive"` | `"watch"`), written only by `init --mode` and `handoff mode`. Not in server.json, not in the plan hash, not part of approval. Changing it preserves workflow, approval, rounds, holds, and Git state.
- Managed A2A only. Legacy repos and A6-era managed repos without the field report `mode: not set`; behavior is unchanged when unset (backward compatible). `handoff mode` on a legacy repo explains that run mode requires managed A2A setup and prints the migration command. `init --transport legacy --mode` is rejected with the same explanation.
- `drive`: `handoff watch` refuses to start, naming `handoff mode <repo> watch`. `handoff execute` stays available (drive uses it).
- `watch`: `handoff execute` stays available because it is the only way to clear a dispatch hold after Planner review and to recover manually; the skill only calls it in that documented case, with human approval. A running watcher re-reads the mode at each poll boundary. If the mode changed to drive, it stops dispatching and exits cleanly with a message after any in-flight run is reconciled. It never interrupts a worker.
- `handoff status` prints a `mode:` line, and its `next:` guidance is mode-aware (drive: "Planner runs execute + QA"; watch: "watch dispatches; Planner does QA").
- Interactive `handoff init` on a TTY also asks for the run mode. Non-interactive new init without `--mode` still succeeds with mode unset and prints the `handoff mode` command. Only provider/model remain mandatory; do not break A6 scripted setup.

Watch in watch mode from the skill: the skill first checks whether a watcher is already running (add a watcher ownership record under `.handoff-logs/service/` with PID plus a start identity, reported by `status`/`status --json`; reuse the service ownership approach from `service.py`, never trust a bare PID). If none is running, the skill gives the human the single `handoff watch "<repo>"` command for a terminal they keep open. It may instead start the watcher as a background process only when the host supports long-lived background commands and the human agrees in chat. Do not build a daemon or auto-start watch from `init` / `server start`.

Machine-readable output:
- `--json` on `models`, `model`, `status`, `server status`, `mode`, and `skill status`. One JSON object on stdout, with a top-level `"schema": "urn:handoff-automation:cli-output:v1"` and a `"kind"` field. Errors keep a nonzero exit and put a JSON `{"error": ...}` on stdout when `--json` is set. Human text output is unchanged.
- Legacy `status --json` must stay Python-free (build with `jq`).
- Never include tokens, credential contents, or provider auth data.

Skill installation and upgrade:
- One installer used by both `handoff skill install` and `init --planner`. Host → project path: cursor `.cursor/skills/handoff-cli`, codex `.agents/skills/handoff-cli`, claude `.claude/skills/handoff-cli` (verify Claude's path first). `--user` installs into the host's user-level skill directory, only when explicitly requested, never as a default. Verify each user path against the installed host/docs; if a path cannot be verified, reject `--user` for that host with a clear message rather than guessing.
- Write an install marker next to the copy (for example `.handoff-skill.json` containing source version and tree digest at install time). States: `absent`; `current` (matches this installation); `outdated` (the tree still matches its own marker digest, i.e. unmodified by the user, but older than the source) → upgraded in place on install/re-init; `foreign` (no marker, digest mismatch with marker, symlink, or tracked) → never overwritten. Treat an A6-era unmarked copy whose digest equals the A6 release SKILL tree digest as `outdated` (record that digest as a known previous release; do not compute from git at runtime).
- Project installs add a precise local git exclude and are excluded from workflow fingerprints (extend `is_workflow_path` and `PLANNER_EXCLUDE` to all three host paths). User installs touch nothing in the repo.
- `handoff planner` keeps working with any `current` or `outdated` project copy (outdated → print the upgrade command).

Executor isolation (must hold with the skill installed in every supported location):
- Add a deny rule for the `handoff` CLI (and `handoff-a2a`) to the Claude Executor allowlist template and to the generated `.claude/settings.local.json` merge, preserving existing entries. Existing projects get it via `handoff permissions` / re-init.
- Verify with fake workers that each adapter's launch environment either does not load the Planner skill or cannot run `handoff`: Cursor (existing deny rules; user-level `~/.cursor/skills` now possible), Codex (user-scope skills already overridden; project `.agents/skills/handoff-cli` now possible), and Claude (project `.claude/skills/handoff-cli`). If an adapter loads project skills natively and there is a supported way to exclude one path for the run, use it; otherwise rely on the deny rule and record that in docs.

Skill content:
- Restructure `skills/handoff-cli/SKILL.md` around intents: Setup, Plan, Approve+Run (mode-aware), QA, Drive loop, Change Executor, Change mode, Status/Recovery, Boundaries. Keep it concise (target ≤ ~200 lines). Move the command catalog and flag details to `skills/handoff-cli/reference.md`, linked one level deep. Parse `--json` output rather than scraping text.
- Setup intent: detect existing setup first (`handoff status --json`, `handoff model --json`, `handoff skill status --json`). Ask only for missing choices: an A6-era repo with no mode set gets only the mode question. Include the A2A default/consent question per "Transport default" above. Then run `init` (fresh) or `mode` (existing), then `server start`, then `server status --json`. Report provider/model/mode/service state and the next chat prompt to use. On sandbox/port/permission denial of `server start` from the agent shell, say so and request the host permission or give the human the single command. Do not misreport it as a product failure.
- Preserve all existing guarantees: DRAFT until chat approval; no self-approval; no invented `handoff plan|qa|drive` verbs; never hand-edit generated JSON; three-execution budget and scope review; human owns merge; Executor must not run the skill.
- `templates/HANDOFF.md` Drive-mode text: mention that the run mode is recorded by the CLI and that the skill is the default entry point. Keep it provider-neutral.

### Files in scope

- `bin/handoff`: `skill` and `mode` dispatch, `--json` forwarding, legacy `status --json` via jq (including `transport`/`managed`), A2A-default TTY transport prompt in `cmd_init`, watch refusal in drive mode for legacy-free managed path, help text restructured to lead with the skill path.
- `src/handoff_a2a/{__main__,config,setup,selection,service,integration,reporting,providers,planner,workspace}.py`: run mode field and command, JSON output, installer/upgrade, watcher ownership record and mode re-check, status `mode:` line. Add at most one new focused module (for example `skills.py` for installer/state, `output.py` for JSON) if it avoids bloating existing modules.
- `skills/handoff-cli/SKILL.md`, new `skills/handoff-cli/reference.md`.
- `templates/HANDOFF.md`, `templates/executor-settings.json`.
- Tests: extend `tests/test_a2a_{setup,cli,cursor_planner,contracts,adapters,selection,workflow}.py` and `tests/a2a_harness.py`; add `tests/test_a2a_skill_install.py` and `tests/test_a2a_run_mode.py` (keep the `test_a2a_` prefix so pytest collects them). Keep `tests/smoke-manifest.sh` 17 checks passing; add legacy checks only for legacy-visible changes (`status --json`, `mode` refusal).
- `scripts/a2a_cursor_live_check.py` (or a new `scripts/a2a_skill_setup_live_check.py`) for the bounded live check.
- Docs: `README.md` (skill-first Quick start and Workflow; command reference moved out), `docs/guides/cli-setup-and-models.md` (full manual CLI reference incl. new commands and JSON schema), `docs/guides/advanced-setup.md` (only if affected), `docs/status/product-status.md`, `docs/history/implementation-plan.md`, new `docs/qa/qa-a7-skill-first-setup.md`, redacted evidence under `docs/evidence/`.
- `pyproject.toml`/`uv.lock` only for a demonstrated need. No SDK/provider upgrades.

Suggested order (phases within one long execution, not separate approval gates): commit the human's pending docs; run mode config + `handoff mode` + status line + watch enforcement; `--json` outputs; installer/upgrade + `skill install/status` + `init --planner` hosts + excludes/fingerprints; Executor isolation deny rule and tests; skill rewrite + reference; help/init output; docs; bounded live check; final regression. Commit each coherent phase with resumable notes.

### Acceptance criteria

1. Bootstrap: in a disposable repo, `handoff skill install --host cursor|codex|claude` installs a working copy at the verified path with marker, local exclude, and fingerprint exclusion; re-run is idempotent; an unmodified older copy (including an A6-era unmarked copy) upgrades; a user-modified, symlinked, tracked, or unrelated copy is never overwritten and the message says how to proceed. `--user` works only for verified host paths and writes nothing into the repo. `skill status` reports all states correctly.
2. Run mode: `init --mode`, `handoff mode` show/set, and interactive init persist the mode via CLI only. Status shows it. Changing mode preserves workflow ID, approval hash, rounds, holds, and HEAD. A6-era config without the field and legacy repos behave as before and say `not set`. Legacy repos stay Python-free.
3. Enforcement: in drive mode `handoff watch` refuses with the fix command. A running watcher (fake worker) exits cleanly after a switch to drive without interrupting or duplicating an in-flight run. In watch mode, `handoff execute` still clears a dispatch hold. Watcher ownership record is reported and stale records are not trusted.
4. JSON: each `--json` command emits one object with the documented schema/kind, correct on success and error paths, no secrets; `status --json` reports `transport` and `managed`; legacy `status --json` works without Python. Interactive `handoff init` accepts Enter as A2A at the transport prompt; non-interactive init behavior is unchanged (existing tests pass). Human-readable output unchanged (existing tests pass).
5. Executor isolation: with the Planner skill installed in each project location (and user location where supported), fake Claude/Codex/Cursor Executor runs cannot invoke `handoff`; the Claude deny rule is generated for new and re-initialized projects and merged without dropping user entries.
6. Skill contract: SKILL.md has the Setup interview (A2A proposed as default with explicit human agreement, legacy fallback with its limits, migration offer for legacy/unmanaged repos, provider, model via `models --json`, Codex-only reasoning effort, run mode, skill location), never silently picks transport/provider/model/mode, checks existing state first, is mode-aware after approval, keeps all existing safety rules, and links `reference.md`. Tests pin the essential rules (not prose wording).
7. Live (bounded, see budget): a Cursor CLI Planner with the installed skill, asked "set up handoff" in a fresh disposable repo, asks the questions (including the A2A-default agreement) without running `init`; given answers that accept A2A, runs `init --transport a2a` with exactly those choices plus `server start`, and reports readiness. Given "plan <tiny task> in the handoff" in watch mode, writes a DRAFT and stops for approval. Cursor Editor discovery/invocation is evidenced or explicitly marked pending with a precise manual step.
8. Docs: README Quick start leads with the bootstrap and chat prompts; the human-facing command list is ≤ the bootstrap commands plus `handoff watch` (for watch mode); the full manual reference lives in `docs/guides/cli-setup-and-models.md`, with every documented command tested. `handoff --help` and `init` output point to the skill first.
9. Regression: full suite and legacy smoke pass; `git diff --check` clean.

### Verification and live-call budget

Develop against disposable repos and fake providers. Do not run setup or live checks in this implementation checkout.

```sh
bash -n bin/handoff
bash tests/smoke-manifest.sh
UV_CACHE_DIR=/private/tmp/handoff-qa-uv-cache uv run --offline --extra test python -m pytest -q
./bin/handoff --help
git diff --check
```

Server/process tests need localhost and process permissions; distinguish sandbox denial from product failures.

Live budget: at most 4 paid model turns total, all Cursor CLI Planner turns in print mode on disposable fixtures: (1) setup request → expect questions only; (2) answers → expect init + server start with those choices; (3) plan request in watch mode → expect DRAFT and stop; (4) one spare for a failed turn, recorded as such. No Executor coding run is required (A6 covered delivery). Record the count and reason before each call; no automatic retries. If Cursor login or a model is unavailable, finish everything else, record the exact pending command, and report the blocker; do not fabricate a pass. Preserve requested/reported model IDs, commands (redacted), fixture Git state, and outputs in durable evidence.

### Completion and scope boundaries

Out of scope: a new orchestration daemon or auto-started watcher; changing the A2A protocol, adapters' launch semantics beyond the isolation deny rule, or approval/round policy; installing skills globally without an explicit `--user`; editing skillshare or other agent configuration; Planner model selection (stays in the Planner host); publishing, push, PR, merge.

Commit coherently on the named branch created from the verified baseline; no push/PR/merge. Preserve unrelated changes. Return READY FOR QA after the agreed checks, or report the remaining external blocker with completed work. The original Planner performs independent diff and behavior QA. Do not self-approve. Corrections follow the existing three-execution policy.

---

## Execution Notes

Not started. Planning only. A6 is archived with its approval and full history in HANDOFF-ARCHIVE.md. This plan is mirrored at docs/history/handoffs/A7-SKILL-FIRST-SETUP.md; root HANDOFF.md is the active coordination surface. If the approved plan changes, synchronize Current Task in both before execution; execution notes and QA thereafter live in root HANDOFF.md.

---

## QA Feedback

Pending implementation and independent Planner QA.
