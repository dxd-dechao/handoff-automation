# A12 QA evidence — polish and sandbox tests

Executor self-report, updated 2026-09-26 after QA round 1. Branch `claude/a12-polish-sandbox-tests`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass (also the first smoke check) |
| `bash tests/smoke-manifest.sh` | 38 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q -m "not server"` | 129 passed, 109 deselected, 103.90s |
| `uv run --offline --extra test python -m pytest -q` | 238 passed, 696.69s (0:11:36) |
| `./bin/handoff --help` | lists `execute` and `resume` with `--wait` |
| `git diff --check` | clean |

The full suite was run on this machine, outside a deny-sandbox. It includes the server-marked tests (localhost, process probes, a started service, a pseudo-terminal). The `not server` count above is also from this machine. `tests/conftest.py` denies `ps`, `kill` of another PID, and localhost bind/connect only inside the pytest process. It cannot reach a child `handoff` process, and it does not simulate a missing pseudo-terminal. Those cases are marked `server` from inspection. The Planner re-runs `pytest -m "not server"` in the real sandbox on the next QA.

## Criteria

1. **Temp-free init.** With `TMPDIR` on a mode `0500` directory, legacy `handoff init` on an existing repo exits 0 and still names `template refresh` when the preamble differs. The same read-only `TMPDIR` on an A2A re-init exits 0, names refresh for a stale preamble, and does not rewrite `HANDOFF.md`. A preamble that matches apart from CRLF does not name refresh.
2. **Mode.** Smoke refresh of a mode `0644` CRLF file leaves it `0644`. `handoff qa` on a mode `0644` mixed-ending file and on a mode `0640` CRLF file keeps those modes.
3. **CRLF.** Smoke: `template refresh` on a CRLF file succeeds, the bytes from `## Current Task` on are unchanged, and the new preamble is CRLF. Init does not hint when the only difference is line endings. `handoff qa` on a CRLF file changes the QA body and the Status value. On a mixed-ending file, bytes outside the QA body and the Status value match the original.
4. **`qa` plan check.** `handoff qa` after a post-approval Current Task edit exits nonzero, names the delivered copy, the pre-QA backup, and re-approve, and writes nothing (no pre-QA backup file).
5. **Rebaseline.** Re-approving a revised plan whose workspace fingerprint equals `post_run_fingerprint` records no `rebaselined_at`. A changed fingerprint still rebaselines. A dirty tree is still refused.
6. **Redundant branch.** `process_start_identity` returns `f"{pid}:unknown"` once when the probe has no identity. Process tests are in the server-marked set and passed in the full suite.
7. **Sandbox tests.** The `server` marker is registered in `pyproject.toml`. It covers process probes (`ps`, `kill 0` on another PID, including a probe run by a child `handoff` process), localhost bind/connect, a started service, and allocating a pseudo-terminal. QA round 1 marked `test_preflight_lists_every_blocker`, `test_watch_retries_a_stopped_service_and_refuses_other_blockers`, and `test_tty_init_accepts_enter_as_a2a_and_asks_the_run_mode`. An audit of the other unmarked subprocess tests found no further case that asserts a service or liveness result from a real `ps` or `kill 0`. `not server`: 129 passed / 109 deselected in 103.90s. Full suite: 238 passed in 696.69s. The in-process simulation does not cover child processes or pty allocation; see the note under Checks.
8. **`--wait`.** `--wait 0` and `--wait abc` exit 1 from bash (smoke). `--wait -5` and `--wait 1801` exit 1 on `execute` and `resume`. `execute --wait 30` and `resume --wait 30` on an 80s fake run each return exit 2 in about 30s with `timeout_s` 30. No flag still uses the configured timeout (the existing 1s timeout test). The skill Drive loop uses `--wait 900`; the watch section does not mention `--wait`.
9. **Follow-ups.** `docs/status/product-status.md` Known follow-ups is "None open."
10. **Regression.** Full pytest and the legacy smoke test passed. JSON kinds and the schema URN are unchanged. The A8 pinned plan hash test passed.
