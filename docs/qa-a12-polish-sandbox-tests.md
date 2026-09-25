# A12 QA evidence — polish and sandbox tests

Executor self-report, 2026-09-25. Branch `claude/a12-polish-sandbox-tests`. Independent Planner QA is still required.

## Checks

| Check | Result |
|---|---|
| `bash -n bin/handoff` | pass (also the first smoke check) |
| `bash tests/smoke-manifest.sh` | 38 passed, 0 failed |
| `uv run --offline --extra test python -m pytest -q -m "not server"` | 131 passed, 106 deselected, 120.09s |
| `uv run --offline --extra test python -m pytest -q` | 237 passed, 704.61s (0:11:44) |
| `./bin/handoff --help` | lists `execute` and `resume` with `--wait` |
| `git diff --check` | clean |

The full suite was run on this machine, outside a deny-sandbox. It includes the server-marked tests (localhost, process probes, a started service).

## Criteria

1. **Temp-free init.** With `TMPDIR` on a mode `0500` directory, legacy `handoff init` on an existing repo exits 0 and still names `template refresh` when the preamble differs. The same read-only `TMPDIR` on an A2A re-init exits 0, names refresh for a stale preamble, and does not rewrite `HANDOFF.md`. A preamble that matches apart from CRLF does not name refresh.
2. **Mode.** Smoke refresh of a mode `0644` CRLF file leaves it `0644`. `handoff qa` on a mode `0644` mixed-ending file and on a mode `0640` CRLF file keeps those modes.
3. **CRLF.** Smoke: `template refresh` on a CRLF file succeeds, the bytes from `## Current Task` on are unchanged, and the new preamble is CRLF. Init does not hint when the only difference is line endings. `handoff qa` on a CRLF file changes the QA body and the Status value. On a mixed-ending file, bytes outside the QA body and the Status value match the original.
4. **`qa` plan check.** `handoff qa` after a post-approval Current Task edit exits nonzero, names the delivered copy, the pre-QA backup, and re-approve, and writes nothing (no pre-QA backup file).
5. **Rebaseline.** Re-approving a revised plan whose workspace fingerprint equals `post_run_fingerprint` records no `rebaselined_at`. A changed fingerprint still rebaselines. A dirty tree is still refused.
6. **Redundant branch.** `process_start_identity` returns `f"{pid}:unknown"` once when the probe has no identity. Process tests are in the server-marked set and passed in the full suite.
7. **Sandbox tests.** The `server` marker is registered in `pyproject.toml` and applied to tests that bind or connect on localhost, start a service, or probe processes. `not server`: 131 passed / 106 deselected in 120.09s. Full suite: 237 passed in 704.61s. The in-process simulation denies `ps`, `kill` of another PID, and localhost bind/connect. A child `handoff` process is not covered by that monkeypatch; those tests are marked from inspection. See Execution Notes.
8. **`--wait`.** `--wait 0` and `--wait abc` exit 1 from bash (smoke). `--wait -5` and `--wait 1801` exit 1 on `execute` and `resume`. `execute --wait 30` and `resume --wait 30` on an 80s fake run each return exit 2 in about 30s with `timeout_s` 30. No flag still uses the configured timeout (the existing 1s timeout test). The skill Drive loop uses `--wait 900`; the watch section does not mention `--wait`.
9. **Follow-ups.** `docs/product-status.md` Known follow-ups is "None open."
10. **Regression.** Full pytest and the legacy smoke test passed. JSON kinds and the schema URN are unchanged. The A8 pinned plan hash test passed.
