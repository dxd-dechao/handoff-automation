"""`handoff server start|status|stop`: owned, verified, conflict-safe local service.

These tests bind loopback ports and start child processes; in a sandbox that
denies local binding they fail at bind, not in the product.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from a2a_harness import field, git, handoff, managed_repo, stop_managed, write_handoff
from handoff_a2a.workspace import parse_handoff

pytestmark = pytest.mark.server


@pytest.fixture
def cleanup():
    repos: list[Path] = []
    yield repos.append
    for repo in repos:
        stop_managed(repo)


def _process(repo: Path) -> dict:
    return json.loads((repo / ".handoff-logs" / "service" / "process.json").read_text())


def _port(repo: Path) -> int:
    return int(json.loads((repo / ".handoff-logs" / "server.json").read_text())["port"])


def _alive(pid: int) -> bool:
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_is_idempotent_verified_and_stop_releases(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    first = handoff(env, "server", "start", str(repo))
    assert first.returncode == 0, first.stderr
    assert field(first.stdout, "service") == "running (verified)"
    assert "cursor / fake-model (generation 1)" in field(first.stdout, "active")
    pid = _process(repo)["pid"]
    again = handoff(env, "server", "start", str(repo))
    assert again.returncode == 0 and "already running" in again.stdout
    assert _process(repo)["pid"] == pid
    status = handoff(env, "server", "status", str(repo))
    assert status.returncode == 0 and "verified" in status.stdout
    assert "test-token" not in json.dumps(_process(repo))
    token = (repo / ".handoff-logs" / "credentials" / "service-token").read_text().strip()
    assert token not in (first.stdout + first.stderr + json.dumps(_process(repo)))
    stopped = handoff(env, "server", "stop", str(repo))
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert not (repo / ".handoff-logs" / "service" / "process.json").exists()
    assert not _alive(pid)
    assert field(handoff(env, "server", "status", str(repo)).stdout, "service") == "stopped"


def test_port_conflict_never_stops_the_foreign_listener(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    foreign = socket.socket()
    foreign.bind(("127.0.0.1", _port(repo)))
    foreign.listen()
    try:
        result = handoff(env, "server", "start", str(repo))
        assert result.returncode == 1
        assert "in use by a process this CLI did not start" in result.stderr
        assert "--port" in result.stderr
        assert not (repo / ".handoff-logs" / "service" / "process.json").exists()
        stop = handoff(env, "server", "stop", str(repo))
        assert stop.returncode == 0 and "left alone" in stop.stdout
        # The listener still works after both commands.
        with socket.create_connection(("127.0.0.1", _port(repo)), timeout=1):
            pass
    finally:
        foreign.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    moved = handoff(env, "server", "start", str(repo), "--port", str(free))
    assert moved.returncode == 0, moved.stderr
    config = json.loads((repo / ".handoff-config.json").read_text())
    assert config["a2a"]["agent_card_url"] == f"http://127.0.0.1:{free}/.well-known/agent-card.json"


def test_stale_record_is_never_signaled(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = repo / ".handoff-logs" / "service" / "process.json"
        record.write_text(
            json.dumps({"pid": bystander.pid, "pgid": bystander.pid, "start_identity": f"{bystander.pid}:another-start", "port": _port(repo)}),
            encoding="utf-8",
        )
        status = handoff(env, "server", "status", str(repo))
        assert "stale" in status.stdout
        started = handoff(env, "server", "start", str(repo))
        assert started.returncode == 0, started.stderr
        assert _process(repo)["pid"] != bystander.pid
        assert bystander.poll() is None  # untouched
    finally:
        bystander.kill()
        bystander.wait()


def test_stop_refuses_unresolved_work(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    pid = _process(repo)["pid"]
    logs = repo / ".handoff-logs"
    for marker in ("outstanding", "execute.lock"):
        if marker == "outstanding":
            (logs / "outstanding.json").write_text("{}\n", encoding="utf-8")
        else:
            (logs / "execute.lock").mkdir()
        refused = handoff(env, "server", "stop", str(repo))
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert "refusing to stop" in refused.stderr and "handoff status" in refused.stderr
        assert _alive(pid)
        if marker == "outstanding":
            (logs / "outstanding.json").unlink()
        else:
            (logs / "execute.lock").rmdir()
    assert handoff(env, "server", "stop", str(repo)).returncode == 0


def test_failed_start_leaves_no_record_and_blocks_dispatch(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "approve", str(repo)).returncode == 0
    failing = {**env, "HANDOFF_A2A_REFUSE_GENERATION": "1"}
    result = handoff(failing, "server", "start", str(repo))
    assert result.returncode == 1
    assert "did not become ready" in result.stderr
    assert not (repo / ".handoff-logs" / "service" / "process.json").exists()
    assert (repo / ".handoff-logs" / "service" / "last-failure.json").is_file()
    # watch/execute will not dispatch to a service that is not verified.
    execute = handoff(env, "execute", str(repo))
    assert execute.returncode != 0
    assert "handoff server start" in execute.stderr
    workflow = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    assert workflow["rounds_used"] == 0 and workflow["reserved_execution_id"] is None
    assert not (repo / ".handoff-logs" / "outstanding.json").exists()


def test_started_server_outlives_the_launching_shell(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    launched = handoff(env, "server", "start", str(repo))
    assert launched.returncode == 0, launched.stderr
    status = handoff(env, "server", "status", str(repo), "--json")
    body = json.loads(status.stdout)
    assert body["verified"] is True and body["state"] == "verified" and body["probe"] == "ok"
    assert _alive(body["pid"])


def test_public_flow_delivers_cursor_run_with_skill_and_repo_guidance(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path, planner="cursor")
    cleanup(repo)
    (repo / "AGENTS.md").write_text("# Repo guidance\nUse tabs.\n", encoding="utf-8")
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-qm", "add repo guidance")
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    assert handoff(env, "approve", str(repo)).returncode == 0
    run = handoff(env, "execute", str(repo))
    assert run.returncode == 0, run.stdout + run.stderr
    assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
    assert (repo / "app.py").read_text() == "value = 1\n"
    probe = json.loads((repo / "DELIVERY_PROBE").read_text())
    assert probe["rule_has_handoff"] is True
    assert probe["planner_skill_present"] is True
    deny = probe["config"]["permissions"]["deny"]
    assert "Shell(handoff)" in deny and "Shell(git:push*)" in deny
    assert probe["config"]["sandbox"] == {"mode": "enabled", "networkAccess": "user_config_only", "networkAllowlist": []}
    argv = json.loads((repo / "ARGV_PROBE").read_text())
    assert argv[-1] == "execute the handoff" and "--force" in argv
    assert (repo / "AGENTS.md").read_text() == "# Repo guidance\nUse tabs.\n"
    assert not list((repo / ".cursor" / "rules").glob("*.mdc")) if (repo / ".cursor" / "rules").exists() else True
    assert git(repo, "status", "--porcelain", "--", ".cursor").stdout.strip() == ""
    manifest = json.loads(next((repo / ".handoff-logs").glob("*-manifest.json")).read_text())
    assert manifest["executor"]["provider"] == "cursor"
    assert manifest["executor"]["model"] == "fake-model"
    assert manifest["executor"]["config_generation"] == 1
    assert manifest["executor"]["reported_model"] == "Fake fake-model"
    runs = handoff(env, "runs", str(repo))
    assert "cursor/fake-model g1" in runs.stdout
    status = handoff(env, "status", str(repo))
    assert "active:   cursor / fake-model (generation 1)" in status.stdout
    stopped = handoff(env, "server", "stop", str(repo))
    assert stopped.returncode == 0, stopped.stderr
