#!/usr/bin/env python3
"""Bounded live A7 check: skill-first setup through a Cursor CLI Planner.

PAID MODEL CALLS. Never run by the default test suite (pytest collects only
tests/test_a2a_*.py) and refuses to start without --confirm-live.

One fresh disposable fixture (no HANDOFF.md, no handoff config). Bootstrap is
the human's one command, `handoff skill install --host cursor` (no model call).
Then at most three Planner turns in one Cursor CLI session (print mode):

  S1  "set up handoff in this repo"  -> must ask the setup questions (A2A
      proposed as the default, asking for agreement; provider; model; run mode)
      and must not run `handoff init`
  S2  the human's answers (A2A yes, cursor / <executor-model>, watch)
      -> must run `init --transport a2a` with exactly those choices, then
      `server start`, and report readiness
  S3  "plan <tiny task> in the handoff" (watch mode) -> must write a DRAFT and
      stop: no approval receipt, no execution, no watcher started

--max-turns (default 4) bounds every sent turn, failures included; the fourth
is a spare for one failed turn. Nothing is retried automatically. The
Planner's shell runs with Cursor's sandbox disabled (setup needs network for
model validation and a local port for the service); dispatch, approval,
publishing, and nested agents are denied by its per-run CLI config.
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
    "GIT_AUTHOR_DATE": "2026-09-25T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-09-25T00:00:00+0000",
}
FIXTURE_FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "mathutil.py": '"""Small math helpers."""\n\n\ndef clamp(value, low, high):\n    raise NotImplementedError\n',
    "test_mathutil.py": (
        "import unittest\n\nfrom mathutil import clamp\n\n\n"
        "class ClampTest(unittest.TestCase):\n"
        "    def test_inside_range_is_unchanged(self):\n"
        "        self.assertEqual(clamp(5, 0, 10), 5)\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n'
    ),
}
SETUP_REQUEST = "/handoff-cli set up handoff in this repo"
PLAN_REQUEST = (
    "/handoff-cli Plan in the handoff for this repository: implement `clamp(value, low, high)` in "
    "mathutil.py (return low if value < low, high if value > high, otherwise value) and add below-range "
    f"and above-range tests to test_mathutil.py. Use branch `{BRANCH}`. Keep it tiny and self-contained for "
    "a headless Executor."
)
DENY = [
    "Shell(handoff:execute*)", "Shell(handoff:watch*)", "Shell(handoff:approve*)", "Shell(handoff:resume*)",
    "Shell(handoff:cancel*)", "Shell(handoff:archive*)",
    "Shell(*/handoff:execute*)", "Shell(*/handoff:watch*)", "Shell(*/handoff:approve*)",
    "Shell(git:push*)", "Shell(git:merge*)", "Shell(git:commit*)", "Shell(gh)",
    "Shell(cursor-agent)", "Shell(agent)", "Shell(claude)", "Shell(codex)",
]
ALLOW = [
    "Read(**)", "Write(HANDOFF.md)", "Shell(handoff)", "Shell(*/handoff)", "Shell(git)", "Shell(ls)",
    "Shell(cat)", "Shell(jq)", "Shell(which)", "Shell(python3)",
]


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def status_of(repo: Path) -> str | None:
    path = repo / "HANDOFF.md"
    if not path.is_file():
        return None
    match = re.search(r"^\*\*Status:\*\*\s*(.*?)\s*$", path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


class Live:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.out).resolve()
        self.repo = self.out / "fixture"
        self.turns: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.session: str | None = None
        self.wrapper = self.out / "handoff-a2a"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CURSOR_", "ANTHROPIC_", "OPENAI_"))}
        self.env.update({"HANDOFF_A2A_BIN": str(self.wrapper), "PATH": f"{HANDOFF_BIN.parent}:{self.env.get('PATH', '')}"})

    def turn(self, label: str, reason: str) -> None:
        if len(self.turns) >= self.args.max_turns:
            raise SystemExit(f"turn budget exhausted before {label}")
        self.turns.append({"n": len(self.turns) + 1, "label": label, "reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        print(f"[paid turn {len(self.turns)}/{self.args.max_turns}] {label}: {reason}", flush=True)

    def handoff(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        result = run([str(HANDOFF_BIN), *args], env=self.env, timeout=timeout)
        self.steps.append({"cmd": ["handoff", *args], "rc": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]})
        print(f"$ handoff {' '.join(args)} -> {result.returncode}", flush=True)
        return result

    def json_of(self, *args: str) -> dict[str, Any]:
        result = self.handoff(*args, "--json")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"unparsed": result.stdout[-2000:], "rc": result.returncode}

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

    def files(self) -> dict[str, Any]:
        config = self.repo / ".handoff-config.json"
        server = self.repo / ".handoff-logs" / "server.json"
        logs = self.repo / ".handoff-logs"
        return {
            "handoff_status": status_of(self.repo),
            "config": json.loads(config.read_text()) if config.is_file() else None,
            "server_selection": {k: v for k, v in json.loads(server.read_text()).items() if k in ("claude", "codex", "cursor", "config_generation")}
            if server.is_file()
            else None,
            "workflow_exists": (logs / "workflow.json").is_file(),
            "manifests": len(list(logs.glob("*-manifest.json"))) if logs.is_dir() else 0,
            "watcher_record": (logs / "service" / "watcher.json").is_file(),
            "git_head": git(self.repo, "rev-parse", "HEAD"),
            "git_branch": git(self.repo, "branch", "--show-current"),
            "git_status": git(self.repo, "status", "--porcelain"),
        }

    def planner(self, label: str, prompt: str, reason: str) -> dict[str, Any]:
        cfg = self.out / f"planner-config-{label}"
        cfg.mkdir()
        (cfg / "cli-config.json").write_text(json.dumps({
            "version": 1,
            "permissions": {"allow": ALLOW, "deny": DENY},
            "approvalMode": "allowlist",
            "sandbox": {"mode": "disabled"},
        }, indent=2))
        binary = shutil.which("cursor-agent") or "cursor-agent"
        argv = [binary, "--print", "--output-format", "stream-json", "--model", self.args.planner_model,
                "--workspace", str(self.repo), "--trust", "--force", "--sandbox", "disabled"]
        if self.session:
            argv += ["--resume", self.session]
        argv.append(prompt)
        before = self.files()
        self.turn(label, reason)
        started = time.monotonic()
        result = run(argv, env={**self.env, "CURSOR_CONFIG_DIR": str(cfg)}, cwd=self.repo, timeout=1800)
        (self.out / f"{label}-stream.jsonl").write_text(result.stdout, encoding="utf-8")
        (self.out / f"{label}-stderr.txt").write_text(result.stderr, encoding="utf-8")
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        final = next((e for e in reversed(events) if e.get("type") == "result"), {})
        self.session = self.session or init.get("session_id") or final.get("session_id")
        calls = [json.dumps(e.get("tool_call") or {})[:600] for e in events if e.get("type") == "tool_call" and e.get("subtype") == "started"]
        return {
            "label": label,
            "argv": argv[:-1] + ["<prompt>"],
            "prompt": prompt,
            "exit_code": result.returncode,
            "duration_s": round(time.monotonic() - started, 1),
            "requested_model": self.args.planner_model,
            "reported_model": init.get("model"),
            "session_id": self.session,
            "is_error": final.get("is_error"),
            "result_text": str(final.get("result") or "")[-4000:],
            "tool_calls": calls,
            "handoff_commands": [c for c in calls if "handoff" in c],
            "before": before,
            "after": self.files(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--out", required=True)
    parser.add_argument("--planner-model", required=True)
    parser.add_argument("--executor-model", required=True, help="Cursor Executor model the scripted human chooses")
    parser.add_argument("--max-turns", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("this sends paid model turns; pass --confirm-live")
    answers = (
        "My answers: 1. Yes, use managed A2A. 2. Executor provider: cursor. "
        f"3. Executor model: {args.executor_model}. 4. Run mode: watch. "
        "5. The skill is already installed in this project; leave it as is. Go ahead with setup."
    )
    live = Live(args)
    results: dict[str, Any] = {"schema": "urn:handoff-automation:a7-live-check:v1", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": {}}
    checks = results["checks"]
    try:
        live.fixture()
        results["initial_commit"] = git(live.repo, "rev-parse", "HEAD")
        results["versions"] = {"cursor-agent": run(["cursor-agent", "--version"]).stdout.strip()}
        install = live.handoff("skill", "install", str(live.repo), "--host", "cursor")
        if install.returncode != 0:
            raise SystemExit(f"bootstrap skill install failed: {install.stderr}")
        results["skill_status"] = live.json_of("skill", "status", str(live.repo))

        s1 = live.planner("S1", SETUP_REQUEST, "setup request: expect the interview, no init")
        results["S1"] = s1
        text = s1["result_text"].lower()
        checks["S1_no_init"] = s1["after"]["config"] is None and s1["after"]["handoff_status"] is None
        checks["S1_asks_a2a_agreement"] = "a2a" in text and ("recommend" in text or "default" in text)
        checks["S1_asks_provider_model_mode"] = all(word in text for word in ("provider", "model", "drive", "watch"))
        if not checks["S1_no_init"]:
            raise SystemExit("S1 ran setup before the human answered")

        s2 = live.planner("S2", answers, "human answers: expect init with exactly those choices + server start")
        results["S2"] = s2
        config = s2["after"]["config"] or {}
        checks["S2_transport_a2a"] = config.get("transport") == "a2a"
        checks["S2_mode_watch"] = (config.get("managed") or {}).get("run_mode") == "watch"
        checks["S2_executor"] = ((s2["after"]["server_selection"] or {}).get("cursor") or {}).get("model") == args.executor_model
        checks["S2_only_cursor_block"] = sorted(k for k in (s2["after"]["server_selection"] or {}) if k in ("claude", "codex", "cursor")) == ["cursor"]
        server = live.json_of("server", "status", str(live.repo))
        results["S2_server_status"] = server
        checks["S2_service_verified"] = server.get("verified") is True
        checks["S2_no_dispatch"] = not s2["after"]["workflow_exists"] and s2["after"]["manifests"] == 0
        results["S2_status"] = live.json_of("status", str(live.repo))

        s3 = live.planner("S3", PLAN_REQUEST, "plan request in watch mode: expect a DRAFT and a stop")
        results["S3"] = s3
        checks["S3_draft"] = s3["after"]["handoff_status"] == "DRAFT"
        checks["S3_no_approval_or_run"] = not s3["after"]["workflow_exists"] and s3["after"]["manifests"] == 0
        checks["S3_no_watcher_started"] = not s3["after"]["watcher_record"]
        checks["S3_code_untouched"] = s3["after"]["git_head"] == results["initial_commit"] and s3["after"]["git_status"] == ""
        results["S3_status"] = live.json_of("status", str(live.repo))
        results["verdict"] = "completed all steps" if all(checks.values()) else "completed with failed checks"
    except SystemExit as exc:
        results["verdict"] = f"stopped: {exc}"
    finally:
        results["turns"] = live.turns
        results["steps"] = live.steps
        if (live.repo / ".handoff-logs" / "server.json").is_file():
            live.handoff("server", "stop", str(live.repo))
        results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        text = json.dumps(results, indent=2, default=str)
        token_file = live.repo / ".handoff-logs" / "credentials" / "service-token"
        if token_file.is_file():
            text = text.replace(token_file.read_text().strip(), "<redacted>")
        (live.out / "results.json").write_text(text + "\n", encoding="utf-8")
        print(f"results: {live.out / 'results.json'}\nverdict: {results['verdict']}\nturns used: {len(live.turns)}")
        print(json.dumps(results.get("checks"), indent=2))
    return 0 if results.get("verdict") == "completed all steps" else 1


if __name__ == "__main__":
    raise SystemExit(main())
