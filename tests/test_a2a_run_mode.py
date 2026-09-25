"""Run mode (drive | watch): `init --mode`, `handoff mode`, status, and watch enforcement.

Tests that start the managed service bind loopback ports; see test_a2a_service.py.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from a2a_harness import HANDOFF_BIN, field, git, handoff, make_repo, managed_env, managed_repo, stop_managed, write_handoff
from handoff_a2a.config import load_config
from handoff_a2a.processes import process_start_identity
from handoff_a2a.workspace import parse_handoff

SCHEMA = "urn:handoff-automation:cli-output:v1"
LEGACY_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


@pytest.fixture
def cleanup():
    repos: list[Path] = []
    yield repos.append
    for repo in repos:
        stop_managed(repo)


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    data = json.loads(result.stdout)
    assert data["schema"] == SCHEMA
    return data


def _config(repo: Path) -> dict:
    return json.loads((repo / ".handoff-config.json").read_text())


def _watch(repo: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(HANDOFF_BIN), "watch", str(repo)],
        env={**env, "HANDOFF_POLL_INTERVAL": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait(predicate, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _legacy_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = LEGACY_PATH
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "no-a2a")
    return env


def test_init_mode_is_persisted_shown_and_changed_without_touching_the_workflow(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    assert load_config(repo).managed.run_mode is None
    assert field(handoff(env, "status", str(repo)).stdout, "mode") == "not set"
    unset = _json(handoff(env, "mode", str(repo), "--json"))
    assert unset["kind"] == "mode" and unset["mode"] is None and unset["managed"] is True
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "approve", str(repo)).returncode == 0
    workflow_file = repo / ".handoff-logs" / "workflow.json"
    workflow = json.loads(workflow_file.read_text())
    workflow["dispatch_hold"] = True
    workflow_file.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
    before = (workflow_file.read_bytes(), (repo / "HANDOFF.md").read_bytes(), git(repo, "rev-parse", "HEAD").stdout)
    server_before = (repo / ".handoff-logs" / "server.json").read_bytes()

    changed = handoff(env, "mode", str(repo), "watch")
    assert changed.returncode == 0, changed.stderr
    assert "mode:    watch (was not set)" in changed.stdout
    assert "workflow, approval, rounds, hold, and Git state are unchanged" in changed.stdout
    assert _config(repo)["managed"]["run_mode"] == "watch"
    again = _json(handoff(env, "mode", str(repo), "drive", "--json"))
    assert again["changed"] is True and again["previous"] == "watch" and again["mode"] == "drive"
    same = _json(handoff(env, "mode", str(repo), "drive", "--json"))
    assert same["changed"] is False
    after = (workflow_file.read_bytes(), (repo / "HANDOFF.md").read_bytes(), git(repo, "rev-parse", "HEAD").stdout)
    assert after == before
    assert (repo / ".handoff-logs" / "server.json").read_bytes() == server_before
    history = [json.loads(line) for line in (repo / ".handoff-logs" / "service" / "history.jsonl").read_text().splitlines()]
    assert [(e["mode"], e["previous"]) for e in history if e["event"] == "run_mode"] == [("watch", None), ("drive", "watch")]
    status = handoff(env, "status", str(repo))
    assert field(status.stdout, "mode") == "drive"
    assert "drive" in field(status.stdout, "next") or "review" in field(status.stdout, "next")
    data = _json(handoff(env, "status", str(repo), "--json"))
    assert data["kind"] == "status" and data["transport"] == "a2a" and data["managed"] is True
    assert data["mode"] == "drive" and data["workflow"]["dispatch_hold"] is True
    assert data["executor"]["selected"] == {
        "provider": "cursor", "model": "fake-model", "reasoning_effort": None, "generation": 1, "validation": "listed"
    }
    token = (repo / ".handoff-logs" / "credentials" / "service-token").read_text().strip()
    for args in (("status",), ("model",), ("mode",), ("server", "status")):
        cmd = [*args[:1], *args[1:], str(repo), "--json"] if args[0] != "server" else ["server", "status", str(repo), "--json"]
        assert token not in handoff(env, *cmd).stdout
    bad = handoff(env, "mode", str(repo), "sometimes")
    assert bad.returncode != 0 and _config(repo)["managed"]["run_mode"] == "drive"


def test_init_with_mode_and_reinit_mode_only(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "HANDOFF.md").unlink()
    env = managed_env(tmp_path)
    result = handoff(env, "init", str(repo), "--transport", "a2a", "--executor", "codex", "--model", "fake-model", "--mode", "watch")
    assert result.returncode == 0, result.stderr
    assert field(result.stdout, "mode") == "watch"
    assert "handoff watch" in result.stdout
    assert _config(repo)["managed"]["run_mode"] == "watch"
    reinit = handoff(env, "init", str(repo), "--mode", "drive")
    assert reinit.returncode == 0, reinit.stderr
    assert "run mode set to drive (was watch)" in reinit.stdout
    assert _config(repo)["managed"]["run_mode"] == "drive"
    # Unset mode on a fresh scripted init stays valid (A6 scripts keep working) and names the command.
    (tmp_path / "other").mkdir()
    other = make_repo(tmp_path / "other")
    (other / "HANDOFF.md").unlink()
    plain = handoff(env, "init", str(other), "--transport", "a2a", "--executor", "cursor", "--model", "fake-model")
    assert plain.returncode == 0, plain.stderr
    assert "run_mode" not in _config(other)["managed"]
    assert f'handoff mode "{other}" <drive|watch>' in plain.stdout


def test_legacy_mode_and_status_json_are_python_free(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "HANDOFF.md").unlink()
    env = _legacy_env(tmp_path)
    rejected = handoff(env, "init", str(repo), "--transport", "legacy", "--mode", "drive")
    assert rejected.returncode == 1 and "run mode requires managed A2A setup" in rejected.stderr
    assert not (repo / "HANDOFF.md").exists()
    assert handoff(env, "init", str(repo), "--transport", "legacy").returncode == 0
    shown = handoff(env, "mode", str(repo))
    assert shown.returncode == 0 and "not set (legacy" in shown.stdout and "--transport a2a" in shown.stdout
    refused = handoff(env, "mode", str(repo), "watch")
    assert refused.returncode == 2 and "requires managed A2A setup" in refused.stderr
    refused_json = handoff(env, "mode", str(repo), "watch", "--json")
    assert refused_json.returncode == 2 and "requires managed A2A setup" in _json(refused_json)["error"]
    shown_json = _json(handoff(env, "mode", str(repo), "--json"))
    assert shown_json["mode"] is None and shown_json["transport"] == "legacy" and shown_json["managed"] is False
    text = (repo / "HANDOFF.md").read_text().replace("**Status:** NO TASK", "**Status:** DRAFT")
    (repo / "HANDOFF.md").write_text(text, encoding="utf-8")
    status = _json(handoff(env, "status", str(repo), "--json"))
    assert status["kind"] == "status" and status["transport"] == "legacy" and status["managed"] is False
    assert status["status"] == "DRAFT" and status["turn"] == "HUMAN" and status["mode"] is None
    assert "handoff approve" in status["next"]
    human = handoff(env, "status", str(repo))
    assert field(human.stdout, "mode") == ""  # legacy human output unchanged
    missing = handoff(env, "status", str(tmp_path / "nowhere"), "--json")
    assert missing.returncode == 1 and "no such directory" in _json(missing)["error"]


def test_a6_era_config_without_mode_behaves_as_before(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "approve", str(repo)).returncode == 0
    status = handoff(env, "status", str(repo))
    assert field(status.stdout, "next") == "handoff execute"
    assert field(status.stdout, "mode") == "not set"
    assert "watcher" not in status.stdout
    config = _config(repo)
    config["managed"]["run_mode"] = "sometimes"
    (repo / ".handoff-config.json").write_text(json.dumps(config), encoding="utf-8")
    broken = handoff(env, "execute", str(repo))
    assert broken.returncode == 1 and "run_mode" in broken.stderr


def test_drive_mode_refuses_watch_with_the_fix(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    assert handoff(env, "mode", str(repo), "drive").returncode == 0
    result = handoff(env, "watch", str(repo), timeout=30)
    assert result.returncode == 2
    assert f'handoff mode "{repo}" watch' in result.stderr
    assert not (repo / ".handoff-logs" / "service" / "watcher.json").exists()


@pytest.mark.server
def test_switch_to_drive_stops_a_watcher_after_its_in_flight_run(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    assert handoff(env, "mode", str(repo), "watch").returncode == 0
    write_handoff(repo, status="DRAFT")
    (repo / ".fake-sleep").write_text("4\n", encoding="utf-8")
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    assert handoff(env, "approve", str(repo)).returncode == 0
    watch = _watch(repo, env)
    try:
        assert _wait(lambda: (repo / "WORKER_STARTED").exists())
        running = _json(handoff(env, "status", str(repo), "--json"))
        assert running["watcher"]["running"] is True and running["watcher"]["pid"]
        second = handoff(env, "watch", str(repo), timeout=30)
        assert second.returncode == 2 and "already running" in second.stderr
        switched = handoff(env, "mode", str(repo), "drive")
        assert switched.returncode == 0 and "stops after its current run" in switched.stdout
        out, _ = watch.communicate(timeout=60)
    finally:
        if watch.poll() is None:
            os.killpg(watch.pid, signal.SIGTERM)
            watch.communicate(timeout=15)
    assert watch.returncode == 0, out
    assert "run mode changed to drive; handoff watch stops" in out
    assert int((repo / "WORKER_LAUNCHES").read_text()) == 1
    assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
    manifests = [json.loads(p.read_text()) for p in (repo / ".handoff-logs").glob("*-manifest.json")]
    assert [m["outcome"] for m in manifests] == ["completed"]
    assert not (repo / ".handoff-logs" / "outstanding.json").exists()
    assert not (repo / ".handoff-logs" / "service" / "watcher.json").exists()
    assert _json(handoff(env, "status", str(repo), "--json"))["watcher"]["running"] is False


@pytest.mark.server
def test_watch_mode_execute_still_clears_a_dispatch_hold(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    assert handoff(env, "mode", str(repo), "watch").returncode == 0
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    assert handoff(env, "approve", str(repo)).returncode == 0
    workflow_file = repo / ".handoff-logs" / "workflow.json"
    workflow = json.loads(workflow_file.read_text())
    workflow["dispatch_hold"] = True
    workflow_file.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
    run = handoff(env, "execute", str(repo))
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(workflow_file.read_text())["dispatch_hold"] is False
    assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
    assert "watch dispatches; the Planner does QA" in field(handoff(env, "status", str(repo)).stdout, "next")


@pytest.mark.server
def test_stale_watcher_record_is_not_trusted(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    record = repo / ".handoff-logs" / "service" / "watcher.json"
    record.write_text(json.dumps({"pid": os.getpid(), "start_identity": "not-this-process", "started_at": "then"}), encoding="utf-8")
    status = handoff(env, "status", str(repo))
    assert "stale watcher record ignored" in status.stdout
    data = _json(handoff(env, "status", str(repo), "--json"))
    assert data["watcher"] == {"running": False, "stale": True, "pid": os.getpid(), "started_at": "then"}
    # A live, verified record (this test process) is reported as running.
    record.write_text(json.dumps({"pid": os.getpid(), "start_identity": process_start_identity(os.getpid()), "started_at": "now"}), encoding="utf-8")
    assert _json(handoff(env, "status", str(repo), "--json"))["watcher"]["running"] is True
    refused = handoff(env, "watch", str(repo), timeout=30)
    assert refused.returncode == 2 and "already running" in refused.stderr
    record.write_text(json.dumps({"pid": 0}), encoding="utf-8")  # garbage is stale, never signaled
    watch = _watch(repo, env)
    try:
        assert _wait(lambda: json.loads(record.read_text()).get("pid") not in (0, None), timeout=20)
    finally:
        os.killpg(watch.pid, signal.SIGTERM)
        watch.communicate(timeout=15)
    assert not record.exists()  # released on SIGTERM


def test_tty_init_accepts_enter_as_a2a_and_asks_the_run_mode(tmp_path: Path) -> None:
    import pty

    repo = make_repo(tmp_path)
    (repo / "HANDOFF.md").unlink()
    env = managed_env(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # child: a real controlling terminal for `read </dev/tty` and input()
        os.execve(str(HANDOFF_BIN), [str(HANDOFF_BIN), "init", str(repo)], env)
    os.write(fd, b"\ncursor\n1\nwatch\nnone\n")
    chunks = []
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    _, status = os.waitpid(pid, 0)
    out = b"".join(chunks).decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 0, out
    assert "Transport [A2A/legacy] (Enter = A2A)" in out
    config = _config(repo)
    assert config["transport"] == "a2a" and config["managed"]["run_mode"] == "watch"
    assert json.loads((repo / ".handoff-logs" / "server.json").read_text())["cursor"]["model"] == "fake-model"
