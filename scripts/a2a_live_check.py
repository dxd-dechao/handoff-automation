#!/usr/bin/env python3
"""Bounded live Executor-replacement check through the real `handoff` CLI.

PAID MODEL CALLS. Never run by the default test suite (pytest collects only
tests/test_a2a_*.py) and refuses to start without --confirm-live.

Three workflows on identical disposable fixtures (same deterministic initial
commit), each capped at three coding executions:

  claude  Claude endpoint: implementation, then one Planner-requested correction
  codex   Codex endpoint: the identical fixture, plan, and correction criteria
  mixed   Claude implements; after reconciliation the endpoint configuration is
          switched to a Codex server, which performs the correction from the
          handoff plus git state only

The script plays the Planner's fixture role (approve, QA feedback, APPROVED)
for the disposable fixture only. It starts and stops its own loopback
servers, and always stops them (and their workers) on exit.

Example:
  uv run --offline python scripts/a2a_live_check.py --confirm-live \
      --out /private/tmp/handoff-live --claude-model bedrock.claude-opus-4-8 \
      --codex-model gpt-5.6-luna --codex-reasoning-effort medium
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_BIN = REPO_ROOT / "bin" / "handoff"
MAX_EXECUTIONS = 3
BRANCH = "live/pricing"
FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "handoff-live-fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "handoff-live-fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-09-24T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-09-24T00:00:00+0000",
}

# ── Fixture ──────────────────────────────────────────────────────────────────

FIXTURE_FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "pricing.py": (
        '"""Order pricing helpers."""\n\n\n'
        "def total(items):\n"
        '    """Return the order total for (unit_price, quantity) pairs."""\n'
        "    raise NotImplementedError\n"
    ),
    "test_pricing.py": (
        "import unittest\n\n"
        "from pricing import total\n\n\n"
        "class TotalTest(unittest.TestCase):\n"
        "    def test_sums_price_times_quantity(self):\n"
        "        self.assertEqual(total([(2.5, 2), (1.0, 3)]), 8.0)\n\n"
        "    def test_empty_order_is_zero(self):\n"
        "        self.assertEqual(total([]), 0)\n\n\n"
        'if __name__ == "__main__":\n'
        "    unittest.main()\n"
    ),
}

CURRENT_TASK = """## Current Task

**Status:** DRAFT

**Branch:** {branch}

### Goal

Implement `total(items)` in `pricing.py` so the existing unit tests pass.

### Steps

1. In `pricing.py`, implement `total(items)`: `items` is a list of `(unit_price, quantity)` pairs; return the sum of `unit_price * quantity` (an empty list returns `0`). Keep the existing signature and docstring.
2. Run `python3 -m unittest -q` from the repository root and make sure it passes.
3. Commit the change on branch `{branch}` with a clear message (`git add pricing.py` then `git commit`).
4. Fill in Execution Notes (what you ran and its result) and set Status to READY FOR QA.

### Acceptance criteria

- `python3 -m unittest -q` passes.
- `pricing.total([(2.5, 2), (1.0, 3)]) == 8.0` and `pricing.total([]) == 0`.
- The implementation is committed on `{branch}`; only `pricing.py` (and, for corrections, `test_pricing.py`) change.

### Out of scope

- No other files, dependencies, or refactors.
- No push, merge, PR, or network access.

---

## Execution Notes

_(empty)_

---

## QA Feedback

_(empty)_
"""

CORRECTION_FEEDBACK = """Round 1 QA (fixture Planner): implementation accepted. One requested correction:

1. `pricing.py`: `total()` must raise `ValueError` when any quantity is negative (a zero quantity stays valid). Fixed looks like: `total([(1.0, -1)])` raises `ValueError`, and all existing behaviour is unchanged.
2. `test_pricing.py`: add a unit test asserting that `ValueError` is raised for a negative quantity.
3. Run `python3 -m unittest -q`, commit both files on the task branch, append to Execution Notes, and set Status to READY FOR QA.
"""


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, check=check, capture_output=True, text=True, timeout=timeout)


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args], check=True).stdout.strip()


def make_fixture(path: Path) -> str:
    """Create the identical disposable fixture; returns its (deterministic) initial commit."""
    path.mkdir(parents=True)
    env = {**os.environ, **FIXED_GIT_ENV}
    run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    for name, body in FIXTURE_FILES.items():
        (path / name).write_text(body, encoding="utf-8")
    run(["git", "-C", str(path), "add", "."], check=True, env=env)
    run(["git", "-C", str(path), "commit", "-qm", "fixture: pricing stub"], check=True, env=env)
    run(["git", "-C", str(path), "checkout", "-qb", BRANCH], check=True, env=env)
    # Executor commits need an identity; keep it local to the fixture.
    git(path, "config", "user.name", "handoff-live-executor")
    git(path, "config", "user.email", "executor@example.invalid")
    return git(path, "rev-parse", "HEAD")


def write_plan(repo: Path) -> None:
    handoff = repo / "HANDOFF.md"
    text = handoff.read_text(encoding="utf-8")
    head = text[: text.index("## Current Task")]
    handoff.write_text(head + CURRENT_TASK.format(branch=BRANCH), encoding="utf-8")


def allow_fixture_tests(repo: Path) -> None:
    """Fixture-local Claude rule for its own test command (README: add build tool commands by hand)."""
    settings = repo / ".claude" / "settings.local.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for rule in ("Bash(python3 -m unittest)", "Bash(python3 -m unittest:*)"):
        if rule not in allow:
            allow.append(rule)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def set_status(repo: Path, status: str, qa: str | None = None) -> None:
    path = repo / "HANDOFF.md"
    text = re.sub(r"^\*\*Status:\*\*.*$", f"**Status:** {status}", path.read_text(encoding="utf-8"), count=1, flags=re.M)
    if qa is not None:
        text = re.sub(r"(## QA Feedback\n).*\Z", lambda m: m.group(1) + "\n" + qa, text, count=1, flags=re.S)
    path.write_text(text, encoding="utf-8")


# ── Independent fixture QA (never trusts the Executor's notes) ───────────────

QA_PROBE = r"""
import json, sys
sys.path.insert(0, ".")
out = {}
try:
    from pricing import total
    out["sum"] = total([(2.5, 2), (1.0, 3)]) == 8.0
    out["empty"] = total([]) == 0
    out["zero_qty_ok"] = total([(3.0, 0)]) == 0
    try:
        total([(1.0, -1)])
        out["negative_raises"] = False
    except ValueError:
        out["negative_raises"] = True
    except Exception:
        out["negative_raises"] = False
except Exception as exc:
    out["import_error"] = repr(exc)
print(json.dumps(out))
"""


def fixture_qa(repo: Path, base: str, *, correction: bool) -> dict[str, Any]:
    probe = run([sys.executable, "-c", QA_PROBE], cwd=repo, timeout=60)
    try:
        values = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        values = {"probe_error": probe.stderr[-500:]}
    tests = run(["python3", "-m", "unittest", "-q"], cwd=repo, timeout=120)
    changed = sorted(set(git(repo, "diff", "--name-only", base).split()))
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").split()
    status = parse_status(repo)
    test_text = (repo / "test_pricing.py").read_text(encoding="utf-8")
    checks = {
        "unit_tests_pass": tests.returncode == 0,
        "sum_correct": values.get("sum") is True,
        "empty_is_zero": values.get("empty") is True,
        "status_ready_for_qa": status == "READY FOR QA",
        "committed_on_branch": git(repo, "branch", "--show-current") == BRANCH
        and git(repo, "rev-list", "--count", f"{base}..HEAD") != "0",
        "only_allowed_files_changed": set(changed) <= {"pricing.py", "test_pricing.py"},
        "no_untracked_code": not untracked,
    }
    if correction:
        checks["negative_qty_raises"] = values.get("negative_raises") is True
        checks["zero_qty_still_valid"] = values.get("zero_qty_ok") is True
        checks["test_added_for_negative"] = "ValueError" in test_text
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "probe": values,
        "unittest_rc": tests.returncode,
        "unittest_tail": (tests.stderr or tests.stdout)[-400:],
        "changed_files": changed,
        "untracked": untracked,
    }


def parse_status(repo: Path) -> str:
    match = re.search(r"^\*\*Status:\*\*\s*(.*)$", (repo / "HANDOFF.md").read_text(encoding="utf-8"), flags=re.M)
    return match.group(1).strip() if match else ""


# ── Servers ──────────────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    """A manually managed loopback `handoff-a2a serve` process owned by this script."""

    def __init__(self, name: str, *, workdir: Path, repo: Path, token_file: Path, workspace_id: str,
                 adapter: dict[str, Any], a2a_bin: Path):
        self.name = name
        self.port = free_port()
        self.evidence = workdir / f"evidence-{name}"
        self.config_path = workdir / f"server-{name}.json"
        self.log_path = workdir / f"server-{name}.log"
        self.config = {
            "host": "127.0.0.1",
            "port": self.port,
            "workspace_id": workspace_id,
            "workspace_path": str(repo),
            "credential_file": str(token_file),
            "evidence_dir": str(self.evidence),
            "execution_timeout_s": 1800,
            "cancel_grace_s": 10,
            **adapter,
        }
        self.a2a_bin = a2a_bin
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def card_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/.well-known/agent-card.json"

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        self.config_path.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [str(self.a2a_bin), "serve", "--config", str(self.config_path)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"server {self.name} exited; see {self.log_path}")
                time.sleep(0.2)
        raise RuntimeError(f"server {self.name} did not start")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        # SIGTERM lets the server's shutdown path stop and reap any owned worker.
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)


async def inspect_card(card_url: str, token: str, task_ids: list[str]) -> dict[str, Any]:
    """Generic SDK check of the advertised card and the standard GetTask operation."""
    from google.protobuf.json_format import MessageToDict

    from handoff_a2a.client import CodingClient, card_supports_durable_dedup
    from handoff_a2a.contracts import CODING_TASK_PROFILE

    client = CodingClient(card_url, token, timeout=30)
    try:
        card_json = await client.connect()
        card = client._card
        assert card is not None
        ext = next(e for e in card.capabilities.extensions if e.uri == CODING_TASK_PROFILE)
        tasks = {}
        for task_id in task_ids:
            task = await client.get(task_id)
            tasks[task_id] = (task.get("status") or {}).get("state")
        return {
            "name": card.name,
            "version": card.version,
            "interfaces": [MessageToDict(i) for i in card.supported_interfaces],
            "profile_required": ext.required,
            "profile_params": MessageToDict(ext.params),
            "durable_dedup": card_supports_durable_dedup(card),
            "security_schemes": sorted(card_json.get("securitySchemes", {}).keys()),
            "get_task_states": tasks,
        }
    finally:
        await client.close()


# ── Workflow driver ──────────────────────────────────────────────────────────


class Workflow:
    def __init__(self, name: str, root: Path, env: dict[str, str], token: str, token_file: Path):
        self.name = name
        self.dir = root / name
        self.repo = self.dir / "repo"
        self.env = env
        self.token = token
        self.token_file = token_file
        self.executions: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.base = ""
        self.stopped: str | None = None

    def handoff(self, *args: str, timeout: float = 2400) -> subprocess.CompletedProcess[str]:
        started = time.time()
        result = run([str(self.env["HANDOFF_EXE"]), *args, str(self.repo)], env=self.env, timeout=timeout)
        self.commands.append({
            "cmd": ["handoff", *args, "<repo>"],
            "rc": result.returncode,
            "seconds": round(time.time() - started, 1),
            "stdout_tail": result.stdout[-1500:],
            "stderr_tail": result.stderr[-1500:],
        })
        return result

    def setup(self) -> None:
        self.base = make_fixture(self.repo)
        init = self.handoff("init")
        if init.returncode != 0:
            raise RuntimeError(f"handoff init failed: {init.stderr}")
        allow_fixture_tests(self.repo)
        write_plan(self.repo)

    def configure(self, server: Server) -> None:
        (self.repo / ".handoff-config.json").write_text(json.dumps({
            "transport": "a2a",
            "max_rounds": MAX_EXECUTIONS,
            "a2a": {
                "agent_card_url": server.card_url,
                "workspace_id": server.config["workspace_id"],
                "credential_file": str(self.token_file),
                "request_timeout_s": 30,
                "wait_timeout_s": 2100,
                "poll_interval_s": 5,
            },
        }, indent=2) + "\n", encoding="utf-8")

    def execute(self, label: str, server: Server, *, correction: bool) -> dict[str, Any]:
        before = set((self.repo / ".handoff-logs").glob("*-manifest.json"))
        started = time.time()
        result = self.handoff("execute")
        after = set((self.repo / ".handoff-logs").glob("*-manifest.json"))
        manifest_files = sorted(after - before)
        manifest = json.loads(manifest_files[-1].read_text()) if manifest_files else {}
        qa = fixture_qa(self.repo, self.base, correction=correction)
        evidence_dir = Path(str((manifest.get("evidence") or {}).get("evidence_dir") or ""))
        model_ids = provider_models(evidence_dir, server.config)
        record = {
            "label": label,
            "endpoint": server.name,
            "endpoint_name": manifest.get("endpoint_name"),
            "rc": result.returncode,
            "wall_s": round(time.time() - started, 1),
            "run_id": manifest.get("run_id"),
            "workflow_id": manifest.get("workflow_id"),
            "execution_id": manifest.get("execution_id"),
            "task_id": manifest.get("task_id"),
            "context_id": manifest.get("context_id"),
            "outcome": manifest.get("outcome"),
            "reason": manifest.get("reason"),
            "status_after": manifest.get("status_after"),
            "usage": manifest.get("usage"),
            "usage_provenance": manifest.get("usage_provenance"),
            "cost_usd": manifest.get("cost_usd"),
            "cost_provenance": manifest.get("cost_provenance"),
            "commits": [c.get("sha") for c in (manifest.get("git") or {}).get("commits") or []],
            "head_after": git(self.repo, "rev-parse", "HEAD"),
            "models": model_ids,
            "qa": qa,
        }
        self.executions.append(record)
        return record

    def qa_round(self, record: dict[str, Any], *, next_feedback: str | None) -> None:
        """Fixture-Planner QA disposition for one execution."""
        if next_feedback is None:
            set_status(self.repo, "APPROVED", qa=f"Fixture QA APPROVED after {record['label']}: all checks passed.\n")
        else:
            set_status(self.repo, "CHANGES REQUESTED", qa=next_feedback)

    def snapshot(self) -> dict[str, Any]:
        logs = self.repo / ".handoff-logs"
        workflow_file = logs / "workflow.json"
        closed = sorted((logs / "closed-workflows").glob("*.json")) if (logs / "closed-workflows").is_dir() else []
        return {
            "fixture_base": self.base,
            "branch_log": git(self.repo, "log", "--format=%h %s", f"{self.base}..HEAD").splitlines(),
            "diff_stat": git(self.repo, "diff", "--stat", self.base),
            "diff": git(self.repo, "diff", self.base, "--", "pricing.py", "test_pricing.py"),
            "working_tree": git(self.repo, "status", "--short"),
            "workflow": json.loads(workflow_file.read_text()) if workflow_file.is_file() else None,
            "closed_workflows": [json.loads(p.read_text()) for p in closed],
            "runs_table": run([str(self.env["HANDOFF_EXE"]), "runs", str(self.repo)], env=self.env).stdout,
            "events": {p.name: p.read_text() for p in sorted(logs.glob("*-events.jsonl"))},
        }


def provider_models(evidence_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Model identity as reported by the provider output, plus the configured one."""
    found: dict[str, Any] = {"configured": (config.get("claude") or config.get("codex") or {}).get("model")}
    stdout = evidence_dir / "stdout.json"
    if not stdout.is_file():
        return found
    raw = stdout.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("modelUsage"), dict):
            found["reported"] = sorted(data["modelUsage"].keys())
    except json.JSONDecodeError:
        models = sorted(set(re.findall(r'"model"\s*:\s*"([^"]+)"', raw)))
        if models:
            found["reported"] = models
    return found


def drive(workflow: Workflow, first: Server, second: Server) -> None:
    """Implementation on `first`, correction on `second` (the same server unless mixed).

    No speculative retries: a failed/canceled delivery stops the workflow for
    diagnosis. The optional third execution is only for a completed delivery
    whose independent QA checks failed (a diagnosed correction).
    """
    workflow.setup()
    first.start()
    try:
        workflow.configure(first)
        approved = workflow.handoff("approve")
        if approved.returncode != 0:
            raise RuntimeError(f"approve failed: {approved.stderr}")
        record = workflow.execute("implementation", first, correction=False)
        if record["outcome"] != "completed":
            workflow.stopped = f"implementation delivery {record['outcome']}: {record['reason']}"
            return
        if not record["qa"]["passed"]:
            failed = [k for k, v in record["qa"]["checks"].items() if not v]
            workflow.qa_round(record, next_feedback=f"Round 1 QA: failing checks {failed}. Fix them.\n\n" + CORRECTION_FEEDBACK)
        else:
            workflow.qa_round(record, next_feedback=CORRECTION_FEEDBACK)
    finally:
        if second is not first or workflow.stopped:
            first.stop()
    correct(workflow, second)


def correct(workflow: Workflow, server: Server) -> None:
    """Correction round(s) on `server`; the endpoint change is configuration only."""
    server.start()
    workflow.configure(server)
    try:
        workflow.commands.append({"note": f"correction endpoint: {server.name}"})
        workflow.handoff("status")
        record = workflow.execute("correction", server, correction=True)
        if (
            record["outcome"] == "completed"
            and not record["qa"]["passed"]
            and len(workflow.executions) < MAX_EXECUTIONS
        ):
            failed = [k for k, v in record["qa"]["checks"].items() if not v]
            workflow.qa_round(record, next_feedback=(
                f"Round {len(workflow.executions)} QA: diagnosed failing checks {failed}. "
                "Fix only these.\n\n" + CORRECTION_FEEDBACK
            ))
            record = workflow.execute("correction-retry", server, correction=True)
        if record["outcome"] != "completed":
            workflow.stopped = f"correction delivery {record['outcome']}: {record['reason']}"
        elif record["qa"]["passed"]:
            workflow.qa_round(record, next_feedback=None)
            workflow.handoff("archive")
        workflow.handoff("status")
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm-live", action="store_true", help="acknowledge paid model calls")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--scenario", choices=["claude", "codex", "mixed", "all"], default="all")
    parser.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--codex-reasoning-effort", default=None)
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("refusing to make paid model calls without --confirm-live")

    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=True)
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    # Temporary installation as the README documents it: this checkout's bin/
    # on PATH (bin/handoff resolves templates relative to its real location),
    # plus a handoff-a2a entry point for the current interpreter.
    handoff_exe = HANDOFF_BIN
    a2a_bin = bindir / "handoff-a2a"
    a2a_bin.write_text(f"#!/usr/bin/env bash\nexec {json.dumps(sys.executable)} -m handoff_a2a \"$@\"\n", encoding="utf-8")
    a2a_bin.chmod(0o755)
    token = secrets.token_urlsafe(32)
    token_file = root / "service-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    env = {k: v for k, v in os.environ.items()}
    env["PATH"] = f"{HANDOFF_BIN.parent}:{bindir}:{env.get('PATH', '')}"
    env["HANDOFF_EXE"] = str(handoff_exe)
    env.pop("HANDOFF_A2A_BIN", None)

    claude_adapter = {"claude": {"binary": args.claude_bin, "model": args.claude_model}}
    codex_block: dict[str, Any] = {"binary": args.codex_bin, "model": args.codex_model}
    if args.codex_reasoning_effort:
        codex_block["reasoning_effort"] = args.codex_reasoning_effort
    codex_adapter = {"codex": codex_block}

    versions = {
        "claude": run([args.claude_bin, "--version"]).stdout.strip(),
        "codex": run([args.codex_bin, "--version"]).stdout.strip() or run([args.codex_bin, "--version"]).stderr.strip(),
        "python": sys.version.split()[0],
        "handoff_commit": git(REPO_ROOT, "rev-parse", "HEAD"),
    }
    scenarios = ["claude", "codex", "mixed"] if args.scenario == "all" else [args.scenario]
    results: dict[str, Any] = {"versions": versions, "scenarios": {}}
    results_path = root / "results.json"

    for name in scenarios:
        workflow = Workflow(name, root, env, token, token_file)
        workflow.dir.mkdir(parents=True, exist_ok=True)
        def server(label: str, adapter: dict[str, Any]) -> Server:
            return Server(f"{name}-{label}", workdir=workflow.dir, repo=workflow.repo, token_file=token_file,
                          workspace_id=f"live-{name}", adapter=adapter, a2a_bin=a2a_bin)
        if name == "claude":
            first = second = server("claude", claude_adapter)
        elif name == "codex":
            first = second = server("codex", codex_adapter)
        else:
            first, second = server("claude", claude_adapter), server("codex", codex_adapter)
        started = time.time()
        error = None
        cards: dict[str, Any] = {}
        try:
            drive(workflow, first, second)
        except Exception as exc:  # noqa: BLE001 — record and continue to the next workflow
            error = repr(exc)
        # Card/GetTask inspection against each endpoint after the fact.
        for srv in {id(first): first, id(second): second}.values():
            ids = [e["task_id"] for e in workflow.executions if e["endpoint"] == srv.name and e["task_id"]]
            try:
                srv.start()
                cards[srv.name] = asyncio.run(inspect_card(srv.card_url, token, ids))
            except Exception as exc:  # noqa: BLE001
                cards[srv.name] = {"error": repr(exc)}
            finally:
                srv.stop()
        results["scenarios"][name] = {
            "error": error,
            "stopped": workflow.stopped,
            "wall_s": round(time.time() - started, 1),
            "executions": workflow.executions,
            "commands": workflow.commands,
            "cards": cards,
            "final": workflow.snapshot() if workflow.repo.exists() else None,
        }
        results_path.write_text(json.dumps(results, indent=2, default=str).replace(token, "<redacted>") + "\n")
        print(f"[{name}] executions={len(workflow.executions)} error={error}", flush=True)
    print(f"results: {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
