#!/usr/bin/env python3
"""Bounded live A6 check: generated setup, Cursor Planner/Executor, model switching.

PAID MODEL CALLS. Never run by the default test suite (pytest collects only
tests/test_a2a_*.py) and refuses to start without --confirm-live.

One disposable fixture, one approved workflow, only public `handoff` commands
for setup/service/selection/execution:

  P1  Cursor CLI Planner drafts a DRAFT (Executor configured for another
      provider, proving role independence); must not approve or dispatch
  --  human approval (this script), branch checkout, immediate switch of the
      Executor to Cursor/<cursor-model>
  E1  Cursor Executor implements via `handoff execute`
  --  human QA feedback; immediate switch to Cursor/<cursor-model-2>
  E2  Cursor Executor, different model, correction
  --  human QA feedback; immediate switch to <cross-provider>/<cross-model>
  E3  cross-provider continuation
  P2  the same Planner session (resumed) performs independent QA of the diff;
      must not dispatch, merge, or self-execute

Every sent model turn counts toward --max-turns (default 6), failures
included; nothing is retried automatically. Planner turns use the Cursor CLI
in print mode (the interactive `handoff planner` launcher cannot be driven by
a script); the launcher's own command is recorded for comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_BIN = REPO_ROOT / "bin" / "handoff"
BRANCH = "live/clamp"
FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "handoff-live-fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "handoff-live-fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-09-24T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-09-24T00:00:00+0000",
}
FIXTURE_FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "AGENTS.md": (
        "# Repository guidance\n\n"
        "- Every commit message in this repository starts with `fixture: `.\n"
        "- Run `python3 -m unittest -q` before committing.\n"
        "- When a task reaches you through a handoff delivery rule, copy its "
        "`Handoff execution:` line verbatim into the Execution Notes you write.\n"
    ),
    "mathutil.py": '"""Small math helpers."""\n\n\ndef clamp(value, low, high):\n    raise NotImplementedError\n',
    "test_mathutil.py": (
        "import unittest\n\nfrom mathutil import clamp\n\n\n"
        "class ClampTest(unittest.TestCase):\n"
        "    def test_inside_range_is_unchanged(self):\n"
        "        self.assertEqual(clamp(5, 0, 10), 5)\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n'
    ),
}
PLANNER_REQUEST = (
    "/handoff-cli Plan in the handoff for this repository: implement `clamp(value, low, high)` in "
    "mathutil.py (return low if value < low, high if value > high, otherwise value) and extend "
    f"test_mathutil.py with below-range and above-range tests. Use branch `{BRANCH}`. Keep it tiny and "
    "self-contained for a headless Executor. Write it as DRAFT and stop for my approval."
)
PLANNER_QA_REQUEST = (
    "/handoff-cli QA the handoff. My approval of the plan was given earlier in this conversation; "
    "the Executor has finished its runs. Review the actual git diff and run the tests, then write QA "
    "Feedback and set the Status yourself. Do not execute, approve an execution, merge, or push."
)
QA_1 = """Round 1 QA (human, live check): implementation accepted. One requested correction:

1. `mathutil.py`: `clamp` must raise `ValueError` when `low > high`. Fixed looks like: `clamp(1, 5, 0)` raises `ValueError`; all other behaviour unchanged.
2. `test_mathutil.py`: add a test asserting that `ValueError`.
3. Run `python3 -m unittest -q`, commit on the task branch, append to Execution Notes, set Status to READY FOR QA.
"""
QA_2 = """Round 2 QA (human, live check): correction accepted. One more small change:

1. `mathutil.py`: add `__all__ = ["clamp"]` directly below the module docstring. Nothing else changes.
2. Run `python3 -m unittest -q`, commit on the task branch, append to Execution Notes, set Status to READY FOR QA.
"""
PLANNER_DENY = [
    "Shell(handoff:execute*)", "Shell(handoff:watch*)", "Shell(handoff:approve*)", "Shell(handoff:resume*)",
    "Shell(handoff:model*)", "Shell(handoff:server*)", "Shell(handoff:archive*)",
    "Shell(*/handoff:execute*)", "Shell(*/handoff:watch*)", "Shell(*/handoff:approve*)",
    "Shell(git:push*)", "Shell(git:merge*)", "Shell(git:commit*)", "Shell(gh)",
    "Shell(cursor-agent)", "Shell(agent)", "Shell(claude)", "Shell(codex)",
]


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def status_of(repo: Path) -> str:
    match = re.search(r"^\*\*Status:\*\*\s*(.*?)\s*$", (repo / "HANDOFF.md").read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def set_status(repo: Path, status: str, qa: str | None = None) -> None:
    path = repo / "HANDOFF.md"
    text = re.sub(r"^\*\*Status:\*\*.*$", f"**Status:** {status}", path.read_text(encoding="utf-8"), count=1, flags=re.M)
    if qa is not None:
        head, sep, _rest = text.partition("## QA Feedback")
        text = head + sep + "\n\n" + qa
    path.write_text(text, encoding="utf-8")


class Live:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.out).resolve()
        self.repo = self.out / "fixture"
        self.turns: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.planner_session: str | None = None
        wrapper = self.out / "handoff-a2a"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CURSOR_", "ANTHROPIC_", "OPENAI_"))}
        self.env.update({"HANDOFF_A2A_BIN": str(wrapper), "PATH": f"{HANDOFF_BIN.parent}:{self.env.get('PATH', '')}"})
        self.wrapper = wrapper

    # ── bookkeeping ──
    def turn(self, label: str, reason: str) -> None:
        if len(self.turns) >= self.args.max_turns:
            raise SystemExit(f"turn budget exhausted before {label}")
        self.turns.append({"n": len(self.turns) + 1, "label": label, "reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        print(f"[paid turn {len(self.turns)}/{self.args.max_turns}] {label}: {reason}", flush=True)

    def handoff(self, *args: str, timeout: float = 3600) -> subprocess.CompletedProcess[str]:
        result = run([str(HANDOFF_BIN), *args], env=self.env, timeout=timeout)
        self.steps.append({"cmd": ["handoff", *args], "rc": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]})
        print(f"$ handoff {' '.join(args)} -> {result.returncode}", flush=True)
        return result

    def git_state(self) -> dict[str, Any]:
        return {
            "branch": git(self.repo, "branch", "--show-current"),
            "head": git(self.repo, "rev-parse", "HEAD"),
            "status": git(self.repo, "status", "--porcelain"),
        }

    def tests(self) -> dict[str, Any]:
        result = run([sys.executable, "-m", "unittest", "-q"], cwd=self.repo, timeout=120)
        return {"rc": result.returncode, "output": (result.stdout + result.stderr)[-1500:]}

    # ── setup ──
    def fixture(self) -> None:
        self.out.mkdir(parents=True, exist_ok=False)
        self.wrapper.write_text(f"#!/usr/bin/env bash\nexec {json.dumps(sys.executable)} -m handoff_a2a \"$@\"\n", encoding="utf-8")
        self.wrapper.chmod(0o755)
        self.repo.mkdir()
        env = {**os.environ, **FIXED_GIT_ENV}
        run(["git", "init", "-q", "-b", "main", str(self.repo)], env=env)
        for name, body in FIXTURE_FILES.items():
            (self.repo / name).write_text(body, encoding="utf-8")
        run(["git", "-C", str(self.repo), "add", "-A"], env=env)
        run(["git", "-C", str(self.repo), "commit", "-qm", "fixture: initial"], env=env)
        git(self.repo, "config", "user.name", "handoff-live-executor")
        git(self.repo, "config", "user.email", "executor@example.invalid")

    def init(self) -> None:
        a = self.args
        init = self.handoff("init", str(self.repo), "--transport", "a2a", "--executor", a.cross_provider, "--model", a.cross_model,
                            *(["--reasoning-effort", a.cross_effort] if a.cross_effort else []), "--planner", "cursor")
        if init.returncode != 0:
            raise SystemExit(f"init failed: {init.stderr}")
        config_path = self.repo / ".handoff-config.json"
        config = json.loads(config_path.read_text())
        config["a2a"]["wait_timeout_s"] = 3000  # live runs take minutes; avoid resume churn
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        start = self.handoff("server", "start", str(self.repo))
        if start.returncode != 0:
            raise SystemExit(f"server start failed: {start.stdout}{start.stderr}")

    # ── Planner turns (Cursor CLI, print mode) ──
    def planner(self, label: str, prompt: str) -> dict[str, Any]:
        cfg = self.out / f"planner-config-{label}"
        cfg.mkdir()
        (cfg / "cli-config.json").write_text(json.dumps({
            "version": 1,
            "permissions": {"allow": ["Read(**)", "Write(HANDOFF.md)", "Shell(git)", "Shell(handoff)", "Shell(python3)"], "deny": PLANNER_DENY},
            "approvalMode": "allowlist",
            "sandbox": {"mode": "enabled", "networkAccess": "user_config_only", "networkAllowlist": []},
        }, indent=2))
        binary = shutil.which("cursor-agent") or "cursor-agent"
        argv = [binary, "--print", "--output-format", "stream-json", "--model", self.args.planner_model,
                "--workspace", str(self.repo), "--trust", "--force", "--sandbox", "enabled"]
        if self.planner_session:
            argv += ["--resume", self.planner_session]
        argv.append(prompt)
        before = self.git_state()
        launches_before = len(list((self.repo / ".handoff-logs").glob("*-manifest.json")))
        self.turn(label, "Cursor CLI Planner " + ("drafts the plan" if not self.planner_session else "QAs the delivered diff (same session)"))
        env = {**self.env, "CURSOR_CONFIG_DIR": str(cfg)}
        started = time.monotonic()
        result = run(argv, env=env, cwd=self.repo, timeout=1800)
        stream = self.out / f"{label}-stream.jsonl"
        stream.write_text(result.stdout, encoding="utf-8")
        (self.out / f"{label}-stderr.txt").write_text(result.stderr, encoding="utf-8")
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        final = next((e for e in reversed(events) if e.get("type") == "result"), {})
        self.planner_session = self.planner_session or init.get("session_id") or final.get("session_id")
        shells = []
        for e in events:
            if e.get("type") == "tool_call" and e.get("subtype") == "started":
                call = e.get("tool_call") or {}
                shells.append(json.dumps(call)[:400])
        return {
            "label": label,
            "argv": argv[:-1] + ["<prompt>"],
            "prompt": prompt,
            "exit_code": result.returncode,
            "duration_s": round(time.monotonic() - started, 1),
            "reported_model": init.get("model"),
            "session_id": self.planner_session,
            "is_error": final.get("is_error"),
            "result_excerpt": str(final.get("result") or "")[-1500:],
            "status_after": status_of(self.repo),
            "tool_calls": shells,
            "git_before": before,
            "git_after": self.git_state(),
            "executions_before": launches_before,
            "executions_after": len(list((self.repo / ".handoff-logs").glob("*-manifest.json"))),
            "workflow_exists": (self.repo / ".handoff-logs" / "workflow.json").is_file(),
            "stream": str(stream),
        }

    # ── Executor turns ──
    def execute(self, label: str, provider: str, model: str, effort: str | None) -> dict[str, Any]:
        switch = self.handoff("model", str(self.repo), "--provider", provider, "--model", model,
                              *(["--reasoning-effort", effort] if effort else []))
        if switch.returncode != 0:
            raise SystemExit(f"switch to {provider}/{model} failed: {switch.stdout}{switch.stderr}")
        workflow_before = json.loads((self.repo / ".handoff-logs" / "workflow.json").read_text())
        before = self.git_state()
        self.turn(label, f"{provider}/{model} Executor via handoff execute")
        started = time.monotonic()
        result = self.handoff("execute", str(self.repo))
        manifests = sorted((self.repo / ".handoff-logs").glob("*-manifest.json"), key=lambda p: p.stat().st_mtime)
        manifest = json.loads(manifests[-1].read_text()) if manifests else {}
        workflow_after = json.loads((self.repo / ".handoff-logs" / "workflow.json").read_text())
        notes = (self.repo / "HANDOFF.md").read_text().split("## Execution Notes", 1)[-1].split("## QA Feedback", 1)[0]
        commits = git(self.repo, "log", "--format=%H %s", f"{before['head']}..HEAD").splitlines()
        return {
            "label": label,
            "switch_stdout": switch.stdout,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr[-2000:],
            "duration_s": round(time.monotonic() - started, 1),
            "outcome": manifest.get("outcome"),
            "reason": manifest.get("reason"),
            "executor": manifest.get("executor"),
            "run_id": manifest.get("run_id"),
            "execution_id": manifest.get("execution_id"),
            "workflow_id": manifest.get("workflow_id"),
            "usage": manifest.get("usage"),
            "cost_usd": manifest.get("cost_usd"),
            "status_after": status_of(self.repo),
            "git_before": before,
            "git_after": self.git_state(),
            "commits": commits,
            "agents_md_commit_prefix": bool(commits) and all(c.split(" ", 1)[1].startswith("fixture: ") for c in commits),
            "execution_id_in_notes": bool(manifest.get("execution_id")) and manifest.get("execution_id") in notes,
            "delivery_rule_left": sorted(p.name for p in (self.repo / ".cursor" / "rules").glob("*.mdc")) if (self.repo / ".cursor" / "rules").is_dir() else [],
            "tests": self.tests(),
            "workflow_unchanged": {k: workflow_before.get(k) == workflow_after.get(k) for k in ("workflow_id", "approved_plan_hash")},
            "rounds_used": workflow_after.get("rounds_used"),
        }

    def evidence_versions(self) -> dict[str, str]:
        versions = {}
        for name, argv in {"cursor-agent": ["cursor-agent", "--version"], "codex": ["codex", "--version"], "claude": ["claude", "--version"]}.items():
            result = run(argv, cwd=self.out, timeout=60)
            versions[name] = [line for line in (result.stdout + result.stderr).splitlines() if line and not line.startswith("WARNING")][:1][0] if result.returncode == 0 else f"unavailable ({result.returncode})"
        return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--out", required=True)
    parser.add_argument("--planner-model", required=True)
    parser.add_argument("--cursor-model", required=True)
    parser.add_argument("--cursor-model-2", required=True)
    parser.add_argument("--cross-provider", choices=("claude", "codex"), required=True)
    parser.add_argument("--cross-model", required=True)
    parser.add_argument("--cross-effort")
    parser.add_argument("--max-turns", type=int, default=6)
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("this sends paid model turns; pass --confirm-live")
    live = Live(args)
    results: dict[str, Any] = {"schema": "urn:handoff-automation:a6-live-check:v1", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        live.fixture()
        results["initial_commit"] = git(live.repo, "rev-parse", "HEAD")
        results["versions"] = live.evidence_versions()
        results["models_listing"] = live.handoff("models", str(live.repo), "--provider", "cursor").stdout.count("\n")
        live.init()
        results["planner_launcher_command"] = live.handoff(
            "planner", str(live.repo), "--provider", "cursor", "--model", args.planner_model, "--print-command").stdout
        p1 = live.planner("P1", PLANNER_REQUEST)
        results["P1"] = p1
        if p1["status_after"] != "DRAFT" or p1["workflow_exists"] or p1["executions_after"]:
            raise SystemExit(f"P1 did not stop at a DRAFT: {p1['status_after']}")
        results["executor_during_P1"] = live.handoff("model", str(live.repo)).stdout
        # Human review of the DRAFT: only normalize a decorated Branch value.
        text = (live.repo / "HANDOFF.md").read_text(encoding="utf-8")
        branch = re.search(r"^\*\*Branch:\*\*\s*(.*?)\s*$", text, re.M)
        results["draft_branch"] = branch.group(1) if branch else None
        if branch and branch.group(1) != BRANCH and branch.group(1).strip("`") == BRANCH:
            (live.repo / "HANDOFF.md").write_text(text.replace(branch.group(0), f"**Branch:** {BRANCH}", 1), encoding="utf-8")
            results["human_draft_edit"] = "normalized Branch value (removed Markdown decoration)"
        approve = live.handoff("approve", str(live.repo))
        if approve.returncode != 0:
            raise SystemExit(f"approve failed: {approve.stderr}")
        git(live.repo, "checkout", "-q", "-b", BRANCH)
        e1 = live.execute("E1", "cursor", args.cursor_model, None)
        results["E1"] = e1
        if e1["outcome"] != "completed":
            raise SystemExit(f"E1 did not complete: {e1['reason']}")
        set_status(live.repo, "CHANGES REQUESTED", QA_1)
        e2 = live.execute("E2", "cursor", args.cursor_model_2, None)
        results["E2"] = e2
        if e2["outcome"] != "completed":
            raise SystemExit(f"E2 did not complete: {e2['reason']}")
        set_status(live.repo, "CHANGES REQUESTED", QA_2)
        e3 = live.execute("E3", args.cross_provider, args.cross_model, args.cross_effort)
        results["E3"] = e3
        if e3["outcome"] != "completed":
            raise SystemExit(f"E3 did not complete: {e3['reason']}")
        results["P2"] = live.planner("P2", PLANNER_QA_REQUEST)
        results["final"] = {"git": live.git_state(), "tests": live.tests(), "status": status_of(live.repo),
                            "diff_stat": git(live.repo, "diff", "--stat", f"{results['initial_commit']}..HEAD")}
        results["verdict"] = "completed all steps"
    except SystemExit as exc:
        results["verdict"] = f"stopped: {exc}"
    finally:
        results["turns"] = live.turns
        results["steps"] = live.steps
        if (live.repo / ".handoff-logs" / "server.json").is_file():
            results["model_final"] = live.handoff("model", str(live.repo)).stdout
            results["runs"] = live.handoff("runs", str(live.repo)).stdout
            results["history"] = (live.repo / ".handoff-logs" / "service" / "history.jsonl").read_text() if (live.repo / ".handoff-logs" / "service" / "history.jsonl").is_file() else ""
            live.handoff("server", "stop", str(live.repo))
        results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        text = json.dumps(results, indent=2, default=str)
        token_file = live.repo / ".handoff-logs" / "credentials" / "service-token"
        if token_file.is_file():
            text = text.replace(token_file.read_text().strip(), "<redacted>")
        (live.out / "results.json").write_text(text + "\n", encoding="utf-8")
        print(f"results: {live.out / 'results.json'}\nverdict: {results['verdict']}\nturns used: {len(live.turns)}")
    return 0 if results.get("verdict") == "completed all steps" else 1


if __name__ == "__main__":
    raise SystemExit(main())
