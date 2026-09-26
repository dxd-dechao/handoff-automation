# A2A Executor replacement: live results (2026-09-24)

Planner review update: retained fixture tests, behavior probes, commits/diffs, and manifest identities were independently verified on 2026-09-24. R1/R2 were accepted at `8d99b93`; A5 closes R3 at `1e09ec5`. The scoped local MVP now passes Planner QA ([final report](../qa/qa-a5-2026-09-24.md)). No paid comparison was repeated for A5. The report below preserves the original experiment and its limitations.

**Outcome:** the real `handoff` CLI ran the same plan and correction on identical disposable fixtures through a Claude endpoint and a Codex endpoint. It also switched from Claude to Codex in the middle of one workflow. Only endpoint configuration changed; the Planner/client code did not. All three workflows passed independent fixture QA for both the implementation and the correction. Codex did so only after two adapter defects, found by the first live attempt, were fixed and verified. The Executor produced these results; independent Planner QA of this product is still pending.

Durable data: [`docs/qa/evidence/a2a-replacement-2026-09-24.json`](../qa/evidence/a2a-replacement-2026-09-24.json). It holds per-execution IDs, usage/cost with provenance, fixture-QA checks, final diffs, Agent Card checks, and command transcripts. Fixtures lived under `/private/tmp/handoff-live-20260924*`; those paths are not the record.

## Setup

| Item | Value |
|---|---|
| Claude CLI | `2.1.281 (Claude Code)`; model `bedrock.claude-opus-4-8` (the legacy `HANDOFF_MODEL` default). The Executor account uses its own stored settings. Model reported in the provider output: `bedrock.claude-opus-4-8` |
| Codex CLI | `codex-cli 0.156.0`; model `gpt-5.6-luna`, `reasoning_effort=medium` (configured; Codex JSONL does not report the model) |
| Python / SDK | 3.13.14; `a2a-sdk==1.1.2` (locked) |
| handoff code | Runs started at `e582fcb` (recorded in the evidence as `ceac9ef`, a content-identical commit later rewritten to drop a stray commit; see Execution Notes). Later executions used the fixed adapter (see "Defects found live") |
| Fixture | Deterministic initial commit `5566e2a` in every workflow: `pricing.py` stub plus `test_pricing.py`; branch `live/pricing` |
| Plan | Implement `total(items)`, run `python3 -m unittest -q`, commit, fill notes, set READY FOR QA |
| Correction (fixture QA feedback) | `total()` raises `ValueError` for a negative quantity (zero stays valid); add a test; commit |
| Fixture QA | Independent script: unit tests, behaviour probes (sum, empty, zero, negative), Status, commit on branch, only allowed files changed, no untracked code. Executor notes are not used as evidence |

Commands (redacted; one explicit entry point makes the paid calls):

```sh
uv run --offline python scripts/a2a_live_check.py --confirm-live \
  --out /private/tmp/handoff-live-20260924 \
  --claude-bin ~/.local/bin/claude --claude-model bedrock.claude-opus-4-8 \
  --codex-bin ~/.local/bin/codex --codex-model gpt-5.6-luna --codex-reasoning-effort medium
# per workflow, through the real CLI (checkout bin/ on a temporary PATH):
handoff init <fixture>; handoff approve <fixture>; handoff execute <fixture>
handoff status <fixture>; handoff resume <fixture>; handoff archive <fixture>
# each endpoint is a manually managed loopback server started by the script:
handoff-a2a serve --config server-<name>.json   # {"claude": {...}} or {"codex": {...}}
```

Endpoint replacement changes only `.handoff-config.json` → `a2a.agent_card_url`. The Planner-side steps, the client, and approval/round logic are the same code for both providers.

## Scenario results

| Workflow (ID) | # | Endpoint | Outcome | Fixture QA | Commit | Worker s | Usage in/out (cache read/create) | Cost (provenance) |
|---|---|---|---|---|---|---|---|---|
| Claude `db3d3381` | 1 impl | Claude | completed | pass | `99be1f7` | 41.9 | 6245/2428 (191400/35902) | $0.412 (`claude.total_cost_usd`) |
| | 2 correction | Claude | completed | pass | `1d9db60` | 63.3 | 6742/3585 (329043/15251) | $0.383 (`claude.total_cost_usd`) |
| Codex, first attempt `84ad6725` | 1 impl | Codex | **failed** | fail | — | 21.5 wall | 57932/497 (43008/—) | null |
| | 2 retry (not diagnosed) | Codex | **failed** | fail | — | 16.7 wall | 43121/406 (33024/—) | null |
| | 3 correction | Codex | **failed** | fail | — | 21.5 wall | 43116/454 (40192/—) | null |
| Mixed `c5a252f5` | 1 impl | Claude | completed (via `handoff resume`) | pass | `9618aad` | 42.3 | 6281/2180 (245092/15234) | $0.304 (`claude.total_cost_usd`) |
| | 2 correction | **Codex** | completed | pass | `b4e1603` | 60.7 | 154369/1817 (125440/—) | null |
| Codex, fresh fixture `e482331c` | 1 impl | Codex | **failed** | partial | — | 25.2 | 62176/589 (52992/—) | null |
| | 2 diagnosed rerun | Codex | completed | pass | `d2a6ef8` | 32.9 | 100616/1066 (79360/—) | null |
| | 3 correction | Codex | completed | pass | `4f9fa84` | 39.4 | 96475/1470 (86528/—) | null |

Worker seconds is `duration_s` from the server's coding result. Where no result was captured, the row shows client wall time. Codex usage comes from `turn.completed.usage`: Codex `input_tokens` includes cached tokens, and `—` means not reported by the adapter version used. Every raw Codex output reported `cache_write_input_tokens: 0`; the adapter now keeps that as a real zero (`6c488e4`). Codex reports no price, so cost stays `null` and nothing is inferred. The Claude cost is the Claude CLI's own `total_cost_usd` for these runs ($1.10 total). Actual gateway billing was not checked.

Final code in all three passing workflows changes only `pricing.py` and `test_pricing.py`. It rejects negative quantities with `ValueError`, keeps zero valid, and adds a test for it. The three implementations differ in style; the behaviour is the same. The Claude and fresh-Codex workflows were archived as approved (2/3 and 3/3 rounds); the mixed workflow was archived at 2/3.

Continuity and correlation (all in the evidence file):

- Each workflow kept one `workflow_id` across executions. Each execution had its own `execution_id`, A2A `task_id`, and `run_id`. Each correction was a new A2A task.
- Mixed workflow `c5a252f5`: Claude task `a23e4be4…`, then Codex task `46657a1d…` on a different server with its own state DB. The Codex correction received only the submitted HANDOFF snapshot plus git state (`9618aad` and the continuation fingerprint). It had no Claude session. The round count continued at 2/3; the approval receipt and plan hash were unchanged.
- An SDK `CodingClient.connect()` against each endpoint verified:
  - the card name: `handoff-a2a Claude Executor` / `handoff-a2a Codex Executor`;
  - the required `urn:handoff-automation:coding-task:v1` extension, with `durable_execution_id_deduplication` and `workspace_code_fingerprint` true and informational `executor_provider`/`executor_model`;
  - the JSON-RPC 1.0 interface and the `localBearer` scheme;
  - standard `GetTask` for every task: COMPLETED, or FAILED for the failed runs.

## Manual recovery observed live

- **Client gone mid-run.** I stopped the driver while the mixed Claude worker was running, which also stopped its waiting `handoff execute` client. `handoff status` then showed Markdown `READY FOR QA` (written early by Claude) but `turn: WAIT`, `execution: WORKING`, rounds `0/3 reserved=…`. After the worker finished, `handoff resume` reconnected to the saved task: one outcome `completed`, rounds 1/3, one manifest. There was no second worker.
- **Failed deliveries.** All four failed Codex executions ended as A2A FAILED. The Status line was restored to the submitted one, and status routed to Planner review with the reason. Nothing reached QA as delivered. The explicit `handoff execute` for the diagnosed rerun cleared the dispatch hold, and the continuation fingerprint accepted the Executor's uncommitted `pricing.py` edit.

## Defects found live (fixed, then verified)

1. **Operator skills hijacked the Codex Executor** (fixed in `93c5d13`). An operator skill in `~/.agents/skills` (`orca-cli`) has a description matching "handoff". Codex treated "execute the handoff" as an agent-handoff request and never opened HANDOFF.md. `--ignore-user-config` does not disable user-scope skills. The adapter now disables every user-scope skill for each run via `skills.config` by SKILL.md path. `codex debug prompt-input` (no model call) confirmed the effect.
2. **Codex could not see HANDOFF.md** (fixed in `e595bb6`). `handoff init` git-excludes HANDOFF.md, and Codex's `rg --files` honours that. On the fresh fixture Codex guessed the task from the code: it implemented `total()` but did not commit, write notes, or set Status. The adapter now sets `project_doc_fallback_filenames=["HANDOFF.md"]`, so Codex loads the validated handoff file itself. The ritual prompt stays fixed and no text is added. `prompt-input` confirmed the handoff reaches the model. The earlier mixed correction had found the file on its own.
3. **The live tool retried without a diagnosis** (tooling; fixed in `93c5d13`). The first Codex workflow spent its 2nd and 3rd executions automatically after an undiagnosed failure. The tool now stops on a failed delivery and allows a third run only after a completed delivery fails its QA checks.
4. **Token counts arrived as floats** in manifests (the protobuf `Struct` transport; fixed in `93c5d13`).
5. **Not fixed, out of scope:** `bin/handoff` finds `templates/` relative to its own path without resolving symlinks, so `handoff init` fails through a symlinked install. The README documents PATH installation, which works.

## Execution count

The plan budgeted six small coding executions across three workflows. **Ten were made.** Three were the first Codex workflow, which exhausted its budget on defects 1 and 3. The fresh Codex workflow then used its first execution on defect 2, a diagnosed rerun, and the correction. No workflow exceeded three executions. No counters were reset: the exhausted first Codex workflow and its failures are kept as evidence. One more small read-only Codex call checked Planner skill invocation (see product-status).

## Limitations

- This shows replacement between two adapters behind **this** server. It does not show interoperability with an independently authored third-party A2A server.
- The fixture is tiny, with one run per scenario. It supports no claim about quality, speed, or cost differences between providers or against direct Claude.
- The permission boundaries differ. Claude uses the target repo's `.claude/settings.local.json` allow/deny rules; for the fixture I added a local `python3 -m unittest` rule, as the README describes. Codex uses its sandbox: workspace plus `.git` write and no network. It can also write temp directories, and it cannot `git push` or reach a remote. Neither is filesystem/account isolation from the operator's user.
- At the live-test revision, native HANDOFF.md loading relied on absence of AGENTS.md. This limitation was corrected in `246981c`: validated handoff delivery alongside AGENTS.md/AGENTS.override.md passed native model-free prompt inspection and independent QA. No new paid live check was run for that correction.
- Codex usage semantics differ from Claude's (cached tokens are included in `input_tokens`), and Codex cost is unknown.
- Destructive interruption (server restart, cancel, deadlines) was verified with the fake-worker tests for both adapters, not with live calls.
