"""Temporary git fixtures and a fake Claude binary for A2A tests."""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn

from handoff_a2a.contracts import CODING_TASK_PROFILE, snapshot_sha256
from handoff_a2a.server import ServerConfig, create_app
from handoff_a2a.adapters.claude import ClaudeAdapterConfig

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time

root = pathlib.Path.cwd()
(root / "WORKER_STARTED").write_text("started\n", encoding="utf-8")
leaked = [k for k in os.environ if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE")]
(root / "AUTH_PROBE").write_text("\n".join(leaked), encoding="utf-8")
mode = (root / ".fake-mode").read_text(encoding="utf-8").strip() if (root / ".fake-mode").is_file() else "success"
sleep_s = float((root / ".fake-sleep").read_text(encoding="utf-8").strip() or "0") if (root / ".fake-sleep").is_file() else 0.0
if sleep_s:
    time.sleep(sleep_s)

handoff_path = root / "HANDOFF.md"
text = handoff_path.read_text(encoding="utf-8")

def write_handoff(new_text):
    handoff_path.write_text(new_text, encoding="utf-8")

def set_status(source, status):
    return re.sub(r"^\*\*Status:\*\*.*$", "**Status:** " + status, source, count=1, flags=re.M)

def set_notes(source, notes):
    return re.sub(
        r"(## Execution Notes\n).*?(?=\n## |\Z)",
        r"\1\n" + notes + "\n\n",
        source,
        count=1,
        flags=re.S,
    )

def set_value(value):
    (root / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], check=True, cwd=root)
    subprocess.run(["git", "commit", "-qm", f"feat: set value to {value}"], cwd=root)

def result_json(**overrides):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10,
        "result": "fake worker done",
        "session_id": "fake-session",
        "total_cost_usd": 0.0,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    payload.update(overrides)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")

if mode == "nonzero":
    sys.stderr.write("boom\n")
    print("this is not json at all")
    sys.exit(3)
if mode == "malformed":
    print("not-json")
    sys.exit(0)
if mode == "wrong_shape":
    set_value(1)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "wrong-shape provider json"))
    json.dump({"unrelated": "not a Claude result"}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)
if mode == "provider_error":
    set_value(1)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "provider error after edits"))
    result_json(is_error=True, result="provider exploded")
    sys.exit(0)
if mode == "cost_missing":
    set_value(1)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "cost omitted"))
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "usage": {"input_tokens": 4, "output_tokens": 5},
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)
if mode == "mangle_plan":
    mangled = text.replace("Set app.py value according to the current round.", "PLANNER TEXT CHANGED")
    write_handoff(set_notes(set_status(mangled, "READY FOR QA"), "changed the plan"))
    result_json()
    sys.exit(0)
if mode == "approved":
    set_value(1)
    write_handoff(set_notes(set_status(text, "APPROVED"), "wrongly approved"))
    result_json()
    sys.exit(0)
if mode == "fix":
    set_value(2)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "addressed QA: value must be 2"))
    result_json()
    sys.exit(0)

set_value(1)
write_handoff(set_notes(set_status(text, "READY FOR QA"), "implemented value=1"))
result_json()
'''


HANDOFF_TEMPLATE = """# HANDOFF.md — fixture

## Current Task

**Status:** {status}

**Branch:** {branch}

### Goal

Set app.py value according to the current round.

### Steps

Edit app.py and leave Execution Notes.

### Acceptance criteria

Independent tests read app.py.

### Out of scope

No publishing.

---

## Execution Notes

{notes}

---

## QA Feedback

{qa}
"""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def write_handoff(
    repo: Path,
    *,
    status: str = "READY FOR EXECUTION",
    branch: str = "main",
    notes: str = "Not started.",
    qa: str = "Not run.",
) -> str:
    text = HANDOFF_TEMPLATE.format(status=status, branch=branch, notes=notes, qa=qa)
    (repo / "HANDOFF.md").write_text(text, encoding="utf-8")
    return text


def make_repo(root: Path, branch: str = "main") -> Path:
    repo = root / "workspace"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("value = 0\n", encoding="utf-8")
    write_handoff(repo, branch=branch)
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "initial fixture")
    return repo


def make_fake_claude(root: Path) -> Path:
    path = root / "fake-claude"
    path.write_text(FAKE_CLAUDE, encoding="utf-8")
    path.chmod(0o755)
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_server_config(
    root: Path,
    repo: Path,
    fake_claude: Path,
    *,
    workspace_id: str = "fixture",
    port: int | None = None,
) -> tuple[ServerConfig, Path, Path]:
    token_path = root / "token"
    token_path.write_text("test-token\n", encoding="utf-8")
    evidence = root / "evidence"
    evidence.mkdir()
    port = port or free_port()
    config = ServerConfig(
        host="127.0.0.1",
        port=port,
        workspace_id=workspace_id,
        workspace_path=repo,
        credential_file=token_path,
        evidence_dir=evidence,
        claude=ClaudeAdapterConfig(binary=str(fake_claude), model="fake-model"),
        public_base_url=f"http://127.0.0.1:{port}",
        credential="test-token",
    )
    return config, token_path, evidence


class RunningServer:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "RunningServer":
        app = create_app(self.config)
        uv_config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_level="error",
            lifespan="on",
        )
        self.server = uvicorn.Server(uv_config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc: object) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)

    @property
    def card_url(self) -> str:
        return f"{self.config.public_base_url}/.well-known/agent-card.json"


def coding_payload(repo: Path, workspace_id: str, **overrides: object) -> dict:
    markdown = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    payload = {
        "schema": CODING_TASK_PROFILE,
        "workflow_id": "wf-1",
        "run_id": "run-1",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "iteration": 1,
        "workspace_id": workspace_id,
        "expected_branch": git(repo, "branch", "--show-current").stdout.strip(),
        "expected_head": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "handoff_markdown": markdown,
        "request_sha256": snapshot_sha256(markdown),
    }
    payload.update(overrides)
    return payload
