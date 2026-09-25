"""Temporary git fixtures and a fake Claude binary for A2A tests."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn

from handoff_a2a.contracts import CODING_TASK_PROFILE, snapshot_sha256
from handoff_a2a.server import ServerConfig, create_app
from handoff_a2a.adapters import ClaudeAdapterConfig, CodexAdapterConfig, CursorAdapterConfig

PROVIDERS = ("claude", "codex", "cursor")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time

# Invoked as `codex exec --json ... --cd <workspace> <prompt>`, as
# `claude -p <prompt> ... --output-format json`, or as
# `cursor-agent --print --output-format stream-json ... --workspace <ws> <prompt>`;
# emit the matching format. Discovery commands (version/status/models) answer
# without touching any workspace.
ARGS = sys.argv[1:]
AUTH_OK = os.environ.get("FAKE_PROVIDER_AUTH", "ok") == "ok"
FAKE_MODELS = [m for m in os.environ.get("FAKE_PROVIDER_MODELS", "fake-model,other-model,grok-4.7-high").split(",") if m]
if ARGS and ARGS[0] in ("--version", "-v"):
    print(os.environ.get("FAKE_PROVIDER_VERSION", "fake-cli 1.0.0"))
    sys.exit(0)
if ARGS[:2] == ["debug", "models"]:
    print(json.dumps({"models": [{"slug": m, "display_name": m.upper()} for m in FAKE_MODELS]}))
    sys.exit(0)
if ARGS and ARGS[0] in ("status", "whoami"):
    print("\u2713 Logged in as fake@example.invalid" if AUTH_OK else "Not logged in")
    sys.exit(0 if AUTH_OK else 1)
if ARGS and (ARGS[0] == "models" or "--list-models" in ARGS):
    if not AUTH_OK:
        print("Authentication required", file=sys.stderr)
        sys.exit(1)
    print("Available models\n")
    for m in FAKE_MODELS:
        print(f"{m} - {m.replace('-', ' ').title()}")
    sys.exit(0)
if ARGS and ARGS[0] not in ("exec", "-p", "--print") and "--print" not in ARGS and "-p" not in ARGS:
    # Interactive launch (e.g. the Cursor Planner): record argv/env and exit.
    probe = os.environ.get("FAKE_INTERACTIVE_PROBE")
    if probe:
        pathlib.Path(probe).write_text(json.dumps({"argv": ARGS, "cwd": os.getcwd()}), encoding="utf-8")
    sys.exit(0)
CODEX = len(sys.argv) > 1 and sys.argv[1] == "exec"
CURSOR = "--print" in ARGS and "--workspace" in ARGS
if CODEX:
    os.chdir(sys.argv[sys.argv.index("--cd") + 1])
if CURSOR:
    os.chdir(ARGS[ARGS.index("--workspace") + 1])
root = pathlib.Path.cwd()
(root / "ARGV_PROBE").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
if CURSOR:
    rules = sorted((root / ".cursor" / "rules").glob("handoff-executor-*.mdc")) if (root / ".cursor" / "rules").is_dir() else []
    cfg_dir = os.environ.get("CURSOR_CONFIG_DIR")
    cfg = pathlib.Path(cfg_dir) / "cli-config.json" if cfg_dir else None
    (root / "DELIVERY_PROBE").write_text(json.dumps({
        "rules": [r.name for r in rules],
        "rule_has_handoff": bool(rules) and (root / "HANDOFF.md").read_text(encoding="utf-8") in rules[0].read_text(encoding="utf-8"),
        "config": json.loads(cfg.read_text(encoding="utf-8")) if cfg and cfg.is_file() else None,
        "planner_skill_present": (root / ".cursor" / "skills" / "handoff-cli" / "SKILL.md").is_file(),
        "model": ARGS[ARGS.index("--model") + 1],
    }), encoding="utf-8")
launches = root / "WORKER_LAUNCHES"
launches.write_text(str(int(launches.read_text(encoding="utf-8") or "0") + 1 if launches.is_file() else 1) + "\n", encoding="utf-8")
(root / "WORKER_STARTED").write_text("started\n", encoding="utf-8")
leaked = [
    k for k in os.environ
    if k.startswith(("ANTHROPIC_", "CLAUDE", "OPENAI_")) or k in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
    or (k.startswith("CURSOR_") and k != "CURSOR_CONFIG_DIR")
]
(root / "AUTH_PROBE").write_text("\n".join(leaked), encoding="utf-8")
mode = (root / ".fake-mode").read_text(encoding="utf-8").strip() if (root / ".fake-mode").is_file() else "success"
sleep_s = float((root / ".fake-sleep").read_text(encoding="utf-8").strip() or "0") if (root / ".fake-sleep").is_file() else 0.0
if mode == "writer":
    marker = root / "CHILD_WRITES"
    script = root / ".child_writer.py"
    script.write_text(
        "import os, pathlib, time\n"
        "p = pathlib.Path(os.environ['CHILD_MARKER'])\n"
        "while True:\n"
        "    p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, str(script)],
        env={**os.environ, "CHILD_MARKER": str(marker)},
    )
    (root / "CHILD_PID").write_text(str(child.pid), encoding="utf-8")
    try:
        time.sleep(sleep_s or 30)
    finally:
        child.wait()
    sys.exit(0)
if mode == "orphan_term_immune":
    marker = root / "CHILD_WRITES"
    script = root / ".child_writer.py"
    script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "p = pathlib.Path(os.environ['CHILD_MARKER'])\n"
        "while True:\n"
        "    p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, str(script)],
        env={**os.environ, "CHILD_MARKER": str(marker)},
    )
    (root / "CHILD_PID").write_text(str(child.pid), encoding="utf-8")
    os._exit(0)
if sleep_s and mode not in ("early_ready", "early_fail", "writer", "orphan_term_immune"):
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

def codex_events(text, is_error=False, usage=None):
    events = [{"type": "thread.started", "thread_id": "fake-thread"}, {"type": "turn.started"}]
    if is_error:
        events.append({"type": "turn.failed", "error": {"message": text}})
    else:
        events.append({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
        events.append({"type": "turn.completed", "usage": usage or {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2}})
    for event in events:
        sys.stdout.write(json.dumps(event) + "\n")

def cursor_events(text, is_error=False, usage=None):
    model = ARGS[ARGS.index("--model") + 1]
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": str(root), "session_id": "fake-session", "model": "Fake " + model, "permissionMode": "default"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "session_id": "fake-session"},
    ]
    result = {"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error, "duration_ms": 10, "duration_api_ms": 5, "result": text, "session_id": "fake-session", "request_id": "fake-request"}
    if usage is not False:
        result["usage"] = usage or {"inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    events.append(result)
    for event in events:
        sys.stdout.write(json.dumps(event) + "\n")

def result_json(**overrides):
    if CODEX:
        codex_events(overrides.get("result", "fake worker done"), overrides.get("is_error", False))
        return
    if CURSOR:
        cursor_events(overrides.get("result", "fake worker done"), overrides.get("is_error", False))
        return
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
    if CODEX:
        codex_events("ok", usage={"input_tokens": 4, "cached_input_tokens": 3, "output_tokens": 5})
        sys.exit(0)
    if CURSOR:
        cursor_events("ok", usage={"inputTokens": 4, "outputTokens": 5})
        sys.exit(0)
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
    (root / "scratch.txt").write_text("uncommitted executor file\n", encoding="utf-8")
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "addressed QA: value must be 2"))
    result_json()
    sys.exit(0)
if mode == "early_fail":
    set_value(1)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "reported early, then failed"))
    extra = float((root / ".fake-sleep").read_text(encoding="utf-8").strip() or "30") if (root / ".fake-sleep").is_file() else 30.0
    time.sleep(extra)
    result_json(is_error=True, result="provider failed late")
    sys.exit(0)
if mode == "early_ready":
    set_value(1)
    write_handoff(set_notes(set_status(text, "READY FOR QA"), "still working"))
    extra = float((root / ".fake-sleep").read_text(encoding="utf-8").strip() or "30") if (root / ".fake-sleep").is_file() else 30.0
    time.sleep(extra)
    result_json()
    sys.exit(0)

set_value(1)
(root / "scratch.txt").write_text("uncommitted executor file\n", encoding="utf-8")
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


def make_fake_claude(root: Path, provider: str = "claude") -> Path:
    path = root / f"fake-{provider}"
    path.write_text(FAKE_CLAUDE, encoding="utf-8")
    path.chmod(0o755)
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_server_json(path: Path, config: ServerConfig) -> Path:
    path.write_text(
        json.dumps(
            {
                "host": config.host,
                "port": config.port,
                "workspace_id": config.workspace_id,
                "workspace_path": str(config.workspace_path),
                "credential_file": str(config.credential_file),
                "evidence_dir": str(config.evidence_dir),
                "state_db": str(config.resolved_state_db()),
                "caller_id": config.caller_id,
                "execution_timeout_s": config.execution_timeout_s,
                "cancel_grace_s": config.cancel_grace_s,
                "public_base_url": config.public_base_url,
                config.executor_name(): {"binary": config.executor().binary, "model": config.executor().model},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def make_server_config(
    tmp_root: Path,
    repo: Path,
    fake_claude: Path,
    *,
    workspace_id: str = "fixture",
    port: int | None = None,
    execution_timeout_s: float = 3600.0,
    cancel_grace_s: float = 1.0,
    state_db: Path | None = None,
    provider: str = "claude",
) -> tuple[ServerConfig, Path, Path]:
    token_path = tmp_root / "token"
    token_path.write_text("test-token\n", encoding="utf-8")
    evidence = tmp_root / "evidence"
    evidence.mkdir(exist_ok=True)
    port = port or free_port()
    config = ServerConfig(
        host="127.0.0.1",
        port=port,
        workspace_id=workspace_id,
        workspace_path=repo,
        credential_file=token_path,
        evidence_dir=evidence,
        claude=ClaudeAdapterConfig(binary=str(fake_claude), model="fake-model") if provider == "claude" else None,
        codex=CodexAdapterConfig(binary=str(fake_claude), model="fake-model") if provider == "codex" else None,
        cursor=CursorAdapterConfig(binary=str(fake_claude), model="fake-model") if provider == "cursor" else None,
        public_base_url=f"http://127.0.0.1:{port}",
        credential="test-token",
        state_db=state_db or (evidence / "state.sqlite"),
        execution_timeout_s=execution_timeout_s,
        cancel_grace_s=cancel_grace_s,
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


# ── Managed CLI fixtures (handoff init / server / model through bin/handoff) ──

HANDOFF_BIN = Path(__file__).resolve().parents[1] / "bin" / "handoff"
PROBE_FILES = (
    ".fake-mode",
    ".fake-sleep",
    "WORKER_STARTED",
    "WORKER_LAUNCHES",
    "AUTH_PROBE",
    "ARGV_PROBE",
    "DELIVERY_PROBE",
    "CHILD_WRITES",
    "CHILD_PID",
    ".child_writer.py",
)


def ignore_probes(repo: Path) -> None:
    (repo / ".gitignore").write_text("\n".join(PROBE_FILES) + "\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore fake worker probes")


def a2a_wrapper(root: Path) -> Path:
    import stat
    import sys

    path = root / "handoff-a2a"
    path.write_text(
        "#!/usr/bin/env bash\n" f"exec {json.dumps(sys.executable)} -m handoff_a2a \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def managed_env(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment whose provider CLIs are all the fake worker binary."""
    import os

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CURSOR_", "OPENAI_")):
            del env[key]
    env["HANDOFF_A2A_BIN"] = str(a2a_wrapper(root))
    for provider in PROVIDERS:
        env[f"HANDOFF_{provider.upper()}_BIN"] = str(make_fake_claude(root, provider))
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def handoff(env: dict[str, str], *args: str, timeout: float = 120.0, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HANDOFF_BIN), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )


def field(output: str, name: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


def stop_managed(repo: Path) -> None:
    """Test cleanup: stop an owned managed server regardless of workflow state."""
    from handoff_a2a.processes import owned_from_record, stop_owned

    record_path = repo / ".handoff-logs" / "service" / "process.json"
    if not record_path.is_file():
        return
    record = json.loads(record_path.read_text(encoding="utf-8"))
    owned = owned_from_record(
        pid=int(record["pid"]), pgid=int(record["pgid"]), start_identity=str(record["start_identity"]), cwd=repo
    )
    stop_owned(owned, grace_s=5.0)
    record_path.unlink(missing_ok=True)


def managed_repo(
    tmp_root: Path,
    *,
    provider: str = "cursor",
    model: str = "fake-model",
    planner: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[Path, dict[str, str]]:
    repo = make_repo(tmp_root)
    ignore_probes(repo)
    (repo / "HANDOFF.md").unlink()  # init installs the template; tests write their own plan
    env = managed_env(tmp_root, extra_env)
    args = ["init", str(repo), "--transport", "a2a", "--executor", provider, "--model", model]
    if planner:
        args += ["--planner", planner]
    result = handoff(env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads((repo / ".handoff-config.json").read_text(encoding="utf-8"))
    config["a2a"].update({"request_timeout_s": 5, "wait_timeout_s": 30, "poll_interval_s": 0.1})
    (repo / ".handoff-config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    server = json.loads((repo / ".handoff-logs" / "server.json").read_text(encoding="utf-8"))
    server["cancel_grace_s"] = 1
    (repo / ".handoff-logs" / "server.json").write_text(json.dumps(server, indent=2) + "\n", encoding="utf-8")
    return repo, env
