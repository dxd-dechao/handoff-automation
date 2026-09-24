from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from a2a_harness import (
    PROVIDERS,
    free_port,
    RunningServer,
    git,
    make_fake_claude,
    make_repo,
    make_server_config,
    write_handoff,
)
from handoff_a2a.workspace import parse_handoff

HANDOFF_BIN = Path(__file__).resolve().parents[1] / "bin" / "handoff"


def _ignore_probes(repo: Path) -> None:
    (repo / ".gitignore").write_text(
        "\n".join(
            [
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
                "",
            ]
        ),
        encoding="utf-8",
    )
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore fake worker probes")


def _wrapper(root: Path) -> Path:
    path = root / "handoff-a2a"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {json.dumps(sys.executable)} -m handoff_a2a \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _env(root: Path, wrapper: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["HANDOFF_A2A_BIN"] = str(wrapper)
    env["HANDOFF_CLAUDE_BIN"] = str(root / "missing-claude")
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if extra:
        env.update(extra)
    return env


def _run(
    repo: Path, env: dict[str, str], *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HANDOFF_BIN), *args, str(repo)],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _write_config(repo: Path, card_url: str, token: Path, workspace_id: str, wait: float = 20.0) -> None:
    (repo / ".handoff-config.json").write_text(
        json.dumps(
            {
                "transport": "a2a",
                "max_rounds": 3,
                "a2a": {
                    "agent_card_url": card_url,
                    "workspace_id": workspace_id,
                    "credential_file": str(token),
                    "request_timeout_s": 5,
                    "wait_timeout_s": wait,
                    "poll_interval_s": 0.1,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _workflow(repo: Path) -> dict:
    return json.loads((repo / ".handoff-logs" / "workflow.json").read_text(encoding="utf-8"))


def test_legacy_execute_without_python_on_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="READY FOR EXECUTION")
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'hello' > hello.txt\n"
        "git add hello.txt\n"
        "git commit -qm 'feat: hello'\n"
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "p = Path('HANDOFF.md')\n"
        "p.write_text(p.read_text().replace('READY FOR EXECUTION', 'READY FOR QA'))\n"
        "print('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"session_id\":\"s\",\"total_cost_usd\":0,"
        "\"usage\":{\"input_tokens\":0,\"output_tokens\":0}}')\n"
        "PY\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["HANDOFF_CLAUDE_BIN"] = str(stub)
    env["PATH"] = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
    result = subprocess.run(
        [str(HANDOFF_BIN), "execute", str(repo)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "READY FOR QA" in (repo / "HANDOFF.md").read_text()


def test_invalid_a2a_config_does_not_fall_back(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="READY FOR EXECUTION")
    (repo / ".handoff-config.json").write_text('{"transport": "nope"}\n', encoding="utf-8")
    env = os.environ.copy()
    env["HANDOFF_CLAUDE_BIN"] = str(tmp_path / "no-claude")
    result = _run(repo, env, "execute")
    assert result.returncode != 0
    assert "unknown" in (result.stderr + result.stdout).lower()
    assert not list((repo / ".handoff-logs").glob("*-manifest.json"))


@pytest.mark.parametrize("provider", PROVIDERS)
def test_approve_execute_correction_and_dirty_refusals(tmp_path: Path, provider: str) -> None:
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path, provider)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, provider=provider)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id)
    draft = _run(repo, env, "execute")
    assert draft.returncode != 0
    with RunningServer(config):
        approved = _run(repo, env, "approve")
        assert approved.returncode == 0, approved.stderr
        workflow_id = _workflow(repo)["workflow_id"]
        first = _run(repo, env, "execute")
        assert first.returncode == 0, first.stderr + first.stdout
        assert (repo / "app.py").read_text() == "value = 1\n"
        assert (repo / "scratch.txt").read_text() == "uncommitted executor file\n"
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
        assert _workflow(repo)["workflow_id"] == workflow_id
        assert _workflow(repo)["rounds_used"] == 1
        write_handoff(
            repo,
            status="CHANGES REQUESTED",
            notes="implemented value=1",
            qa="app.py must set value = 2, not 1.",
        )
        (repo / ".fake-mode").write_text("fix\n", encoding="utf-8")
        second = _run(repo, env, "execute")
        assert second.returncode == 0, second.stderr
        assert (repo / "app.py").read_text() == "value = 2\n"
        assert (repo / "scratch.txt").read_text() == "uncommitted executor file\n"
        assert _workflow(repo)["workflow_id"] == workflow_id
        assert _workflow(repo)["rounds_used"] == 2
        manifests = sorted((repo / ".handoff-logs").glob("*-manifest.json"))
        assert len(manifests) == 2
        for path in manifests:
            body = path.read_text(encoding="utf-8")
            assert "test-token" not in body
            manifest = json.loads(body)
            assert manifest["workflow_id"] == workflow_id
            assert manifest["endpoint_name"].lower().endswith(f"{provider} executor")
        assert len({json.loads(m.read_text())["task_id"] for m in manifests}) == 2
    assert (repo / "AUTH_PROBE").read_text() == ""


def test_unapproved_dirty_and_server_fingerprint_reject(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id)
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        (repo / "sneaky.py").write_text("nope = 1\n", encoding="utf-8")
        dirty = _run(repo, env, "execute")
        assert dirty.returncode != 0
        assert "clean code baseline" in dirty.stderr
        (repo / "sneaky.py").unlink()
        hold = tmp_path / "hold"
        hold.write_text("1\n", encoding="utf-8")
        env_hold = _env(tmp_path, wrapper, {"HANDOFF_A2A_HOLD_AFTER_RECORD": str(hold)})
        proc = subprocess.Popen(
            [str(HANDOFF_BIN), "execute", str(repo)],
            env=env_hold,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        outstanding = repo / ".handoff-logs" / "outstanding.json"
        deadline = time.time() + 10
        while time.time() < deadline and not outstanding.is_file():
            time.sleep(0.05)
        assert outstanding.is_file()
        (repo / "intervening.py").write_text("edited\n", encoding="utf-8")
        hold.unlink()
        _stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode != 0
        assert (repo / "intervening.py").read_text() == "edited\n"
        combined = stderr + _stdout
        assert "rejected" in combined.lower()
        assert not (repo / "WORKER_LAUNCHES").exists()
        assert _workflow(repo)["rounds_used"] == 0


def test_duplicate_dispatch_and_legacy_switch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("early_ready\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("8\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id)
    hold = tmp_path / "hold"
    hold.write_text("1\n", encoding="utf-8")
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        env_hold = _env(tmp_path, wrapper, {"HANDOFF_A2A_HOLD_AFTER_RECORD": str(hold)})
        first = subprocess.Popen(
            [str(HANDOFF_BIN), "execute", str(repo)],
            env=env_hold,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        outstanding = repo / ".handoff-logs" / "outstanding.json"
        deadline = time.time() + 10
        while time.time() < deadline and not outstanding.is_file():
            time.sleep(0.05)
        second = _run(repo, env, "execute")
        assert second.returncode != 0
        assert "outstanding" in second.stderr
        (repo / ".handoff-config.json").unlink()
        stub = tmp_path / "legacy-claude"
        stub.write_text("#!/usr/bin/env bash\necho should-not-run >&2\nexit 9\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        legacy_env = os.environ.copy()
        legacy_env["HANDOFF_CLAUDE_BIN"] = str(stub)
        switched = subprocess.run(
            [str(HANDOFF_BIN), "execute", str(repo)],
            capture_output=True,
            text=True,
            env=legacy_env,
        )
        assert switched.returncode != 0
        assert "outstanding" in switched.stderr
        _write_config(repo, card, token_path, config.workspace_id)
        hold.unlink()
        out, err = first.communicate(timeout=40)
        assert first.returncode == 0, err + out
        assert len(list((repo / ".handoff-logs").glob("*-manifest.json"))) == 1
        resume = _run(repo, env, "resume")
        assert resume.returncode != 0
        assert "no outstanding" in resume.stderr


def test_early_ready_stays_wait_and_timeout_is_resumable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("early_ready\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("6\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id, wait=1.0)
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        timed = _run(repo, env, "execute")
        assert timed.returncode == 2
        assert (repo / ".handoff-logs" / "outstanding.json").is_file()
        status = _run(repo, env, "status")
        assert "WAIT" in status.stdout
        assert "READY FOR QA" in (repo / "HANDOFF.md").read_text()
        _write_config(repo, card, token_path, config.workspace_id, wait=20)
        resumed = _run(repo, env, "resume")
        assert resumed.returncode == 0, resumed.stderr
        assert not (repo / ".handoff-logs" / "outstanding.json").exists()
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
        assert _workflow(repo)["rounds_used"] == 1


def test_round_limit_mixed_runs_and_successor(tmp_path: Path) -> None:
    space_root = tmp_path / "work space"
    space_root.mkdir()
    repo = make_repo(space_root)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("nonzero\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id)
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        for _ in range(3):
            result = _run(repo, env, "execute")
            assert result.returncode == 1, result.stderr
        assert _workflow(repo)["rounds_used"] == 3
        fourth = _run(repo, env, "execute")
        assert fourth.returncode != 0
        assert "round" in fourth.stderr.lower() or "scope" in fourth.stderr.lower()
        mixed = repo / ".handoff-logs" / "legacy-manifest.json"
        mixed.write_text(
            json.dumps(
                {
                    "run_id": "legacy-run",
                    "finished_at": "2026-01-01T00:00:00+0000",
                    "wall_duration_s": 1,
                    "exit_code": 0,
                    "status_after": "READY FOR QA",
                    "claude": {
                        "total_cost_usd": 0,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                    "git": {"commits": []},
                }
            ),
            encoding="utf-8",
        )
        mixed_out = _run(repo, env, "runs")
        assert "legacy-run" in mixed_out.stdout
        assert "$0" in mixed_out.stdout
        archived = subprocess.run(
            [str(HANDOFF_BIN), "archive", "--superseded", str(repo)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert archived.returncode == 0, archived.stderr
        assert "Disposition: superseded" in (repo / "HANDOFF-ARCHIVE.md").read_text()
        write_handoff(repo, status="DRAFT", qa="one remaining file")
        successor = _run(repo, env, "approve")
        assert successor.returncode == 0, successor.stderr
        assert _workflow(repo)["parent_workflow_id"]
        assert _workflow(repo)["rounds_used"] == 0



def _start_early_ready_run(tmp_path: Path, sleep_s: int = 25, provider: str = "claude"):
    """Approve and execute a fake worker that writes READY FOR QA early and keeps running."""
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path, provider)
    (repo / ".fake-mode").write_text("early_ready\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text(f"{sleep_s}\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, provider=provider)
    wrapper = _wrapper(tmp_path)
    env = _env(tmp_path, wrapper)
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id, wait=1.0)
    return repo, config, token_path, env, card


def _wait_for_worker(repo: Path) -> None:
    deadline = time.time() + 15
    while time.time() < deadline and not (repo / "WORKER_STARTED").exists():
        time.sleep(0.05)
    assert (repo / "WORKER_STARTED").exists()


def _field(output: str, name: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


def test_config_switch_to_legacy_keeps_outstanding_run_authoritative(tmp_path: Path) -> None:
    repo, config, token_path, env, card = _start_early_ready_run(tmp_path, sleep_s=8)
    lock = repo / ".handoff-logs" / "execute.lock"
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        timed = _run(repo, env, "execute")
        assert timed.returncode == 2, timed.stderr + timed.stdout
        _wait_for_worker(repo)
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"

        (repo / ".handoff-config.json").write_text('{"transport": "legacy"}\n', encoding="utf-8")
        status = _run(repo, env, "status")
        assert _field(status.stdout, "turn") == "WAIT", status.stdout + status.stderr
        assert _field(status.stdout, "execution") == "WORKING"
        assert lock.is_dir()

        # New dispatch stays blocked on either transport.
        blocked = _run(repo, env, "execute")
        assert blocked.returncode != 0 and "outstanding" in blocked.stderr
        assert _run(repo, env, "approve").returncode != 0

        # Selecting a different endpoint must not reroute the saved run.
        dead = f"http://127.0.0.1:{free_port()}/.well-known/agent-card.json"
        _write_config(repo, dead, token_path, config.workspace_id, wait=30.0)
        status = _run(repo, env, "status")
        assert _field(status.stdout, "turn") == "WAIT", status.stdout + status.stderr

        (repo / ".handoff-config.json").write_text('{"transport": "legacy"}\n', encoding="utf-8")
        resumed = _run(repo, env, "resume")
        assert resumed.returncode == 0, resumed.stderr + resumed.stdout
        assert '"outcome": "completed"' in resumed.stdout
        assert not (repo / ".handoff-logs" / "outstanding.json").exists()
        assert not lock.exists()
        assert _workflow(repo)["rounds_used"] == 1
        assert (repo / "WORKER_LAUNCHES").read_text().strip() == "1"
        assert len(list((repo / ".handoff-logs").glob("*-manifest.json"))) == 1

        # Reconciled: legacy status is Python-free again and routes to Planner QA.
        after = _run(repo, {**env, "HANDOFF_A2A_BIN": str(tmp_path / "no-a2a")}, "status")
        assert after.returncode == 0, after.stderr
        assert _field(after.stdout, "turn") == "PLANNER"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_config_removed_cancel_uses_saved_run_and_releases_once(tmp_path: Path, provider: str) -> None:
    repo, config, token_path, env, card = _start_early_ready_run(tmp_path, sleep_s=25, provider=provider)
    lock = repo / ".handoff-logs" / "execute.lock"
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        assert _run(repo, env, "execute").returncode == 2
        _wait_for_worker(repo)
        (repo / ".handoff-config.json").unlink()

        status = _run(repo, env, "status")
        assert _field(status.stdout, "turn") == "WAIT", status.stdout + status.stderr
        assert lock.is_dir()

        # Saved credential unavailable: visible unresolved state, nothing released.
        moved = token_path.with_name("token.moved")
        token_path.rename(moved)
        missing = _run(repo, env, "status")
        assert missing.returncode == 2
        assert _field(missing.stdout, "turn") == "WAIT"
        assert "credential" in _field(missing.stdout, "reason")
        assert _run(repo, env, "resume").returncode == 2
        assert _run(repo, env, "cancel").returncode == 2
        assert (repo / ".handoff-logs" / "outstanding.json").is_file()
        assert lock.is_dir()
        moved.rename(token_path)

        canceled = _run(repo, env, "cancel")
        assert '"outcome": "canceled"' in canceled.stdout, canceled.stdout + canceled.stderr
        assert not (repo / ".handoff-logs" / "outstanding.json").exists()
        assert not lock.exists()
        workflow = _workflow(repo)
        assert workflow["rounds_used"] == 1
        assert workflow["reserved_execution_id"] is None
        assert workflow["dispatch_hold"] is True
        assert (repo / "WORKER_LAUNCHES").read_text().strip() == "1"
        manifests = list((repo / ".handoff-logs").glob("*-manifest.json"))
        assert len(manifests) == 1
        assert json.loads(manifests[0].read_text())["outcome"] == "canceled"
        # A second cancel has nothing to act on.
        again = _run(repo, env, "cancel")
        assert again.returncode != 0


def _watch_for(repo: Path, env: dict[str, str], until, timeout: float = 30.0) -> str:
    """Run the public `handoff watch` until `until()` holds (or timeout), then stop it."""
    import signal

    proc = subprocess.Popen(
        [str(HANDOFF_BIN), "watch", str(repo)],
        env={**env, "HANDOFF_POLL_INTERVAL": "1", "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not until():
            time.sleep(0.2)
        time.sleep(2.5)  # at least two more polls: nothing further may be dispatched
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    return out


def _setup_mode(
    tmp_path: Path, mode: str, *, sleep_s: int | None = None, wait: float = 20.0, provider: str = "claude"
):
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path, provider)
    (repo / ".fake-mode").write_text(f"{mode}\n", encoding="utf-8")
    if sleep_s is not None:
        (repo / ".fake-sleep").write_text(f"{sleep_s}\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, provider=provider)
    env = _env(tmp_path, _wrapper(tmp_path))
    write_handoff(repo, status="DRAFT")
    card = f"http://127.0.0.1:{config.port}/.well-known/agent-card.json"
    _write_config(repo, card, token_path, config.workspace_id, wait=wait)
    return repo, config, env


def _launches(repo: Path) -> int:
    path = repo / "WORKER_LAUNCHES"
    return int(path.read_text().strip()) if path.is_file() else 0



@pytest.mark.parametrize(
    ("mode", "written", "provider"),
    [
        ("provider_error", "READY FOR QA", "claude"),
        ("provider_error", "READY FOR QA", "codex"),
        ("approved", "APPROVED", "claude"),
        ("mangle_plan", "READY FOR QA", "claude"),
    ],
)
def test_failed_delivery_is_not_routed_to_qa_and_watch_holds(
    tmp_path: Path, mode: str, written: str, provider: str
) -> None:
    repo, config, env = _setup_mode(tmp_path, mode, provider=provider)
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        failed = _run(repo, env, "execute")
        assert failed.returncode == 1, failed.stderr + failed.stdout
        document = parse_handoff((repo / "HANDOFF.md").read_text())
        assert document.status == "READY FOR EXECUTION"
        assert document.execution_notes  # executor evidence kept
        status = _run(repo, env, "status")
        assert _field(status.stdout, "turn") == "PLANNER", status.stdout
        assert "review of failed delivery" in _field(status.stdout, "next")
        assert repr(written) in status.stdout
        if mode == "mangle_plan":
            assert "changed since approval" in status.stdout
        manifest = json.loads(next((repo / ".handoff-logs").glob("*-manifest.json")).read_text())
        assert manifest["outcome"] == "failed"
        assert manifest["executor_status_discarded"] == written
        out = _watch_for(repo, env, until=lambda: False, timeout=2)
        assert "watch will not retry" in out, out
        assert _launches(repo) == 1
        assert _workflow(repo)["rounds_used"] == 1


def test_watch_after_wait_timeout_reconciles_once_and_notifies(tmp_path: Path) -> None:
    repo, config, env = _setup_mode(tmp_path, "early_ready", sleep_s=4, wait=1.0)
    outstanding = repo / ".handoff-logs" / "outstanding.json"
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        assert _run(repo, env, "execute").returncode == 2
        assert outstanding.is_file()
        out = _watch_for(repo, env, until=lambda: not outstanding.exists())
        assert not outstanding.exists(), out
        assert out.count("NOTIFY: Handoff: READY FOR QA") == 1, out
        assert _launches(repo) == 1
        assert _workflow(repo)["rounds_used"] == 1
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"


def test_watch_after_cancel_does_not_redispatch(tmp_path: Path) -> None:
    repo, config, env = _setup_mode(tmp_path, "early_ready", sleep_s=25, wait=1.0)
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        assert _run(repo, env, "execute").returncode == 2
        _wait_for_worker(repo)
        canceled = _run(repo, env, "cancel")
        assert '"outcome": "canceled"' in canceled.stdout, canceled.stdout + canceled.stderr
        # The worker's early READY FOR QA is not accepted from a canceled run.
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR EXECUTION"
        out = _watch_for(repo, env, until=lambda: False, timeout=2)
        assert "watch will not retry" in out, out
        assert _launches(repo) == 1
        assert not (repo / ".handoff-logs" / "outstanding.json").exists()


def test_constrained_successor_executes_on_inherited_uncommitted_work(tmp_path: Path) -> None:
    repo, config, env = _setup_mode(tmp_path, "success")
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        assert _run(repo, env, "execute").returncode == 0
        assert (repo / "scratch.txt").is_file()  # uncommitted executor work
        parent = _workflow(repo)["workflow_id"]
        (repo / ".fake-mode").write_text("nonzero\n", encoding="utf-8")
        for _ in range(2):
            write_handoff(repo, status="CHANGES REQUESTED", notes="implemented value=1", qa="value must be 2")
            assert _run(repo, env, "execute").returncode == 1
        assert _workflow(repo)["rounds_used"] == 3
        status = _run(repo, env, "status")
        assert "scope review" in _field(status.stdout, "next"), status.stdout
        archived = subprocess.run(
            [str(HANDOFF_BIN), "archive", "--superseded", str(repo)], capture_output=True, text=True, env=env
        )
        assert archived.returncode == 0, archived.stderr
        closed = json.loads((repo / ".handoff-logs" / "closed-workflows" / f"{parent}.json").read_text())
        assert closed["rounds_used"] == 3 and closed["disposition"] == "superseded"

        write_handoff(repo, status="DRAFT", notes="Not started.", qa="Successor: only set value = 2.")
        assert _run(repo, env, "approve").returncode == 0
        successor = _workflow(repo)
        assert successor["parent_workflow_id"] == parent
        assert successor["rounds_used"] == 0
        assert successor["inherited_baseline"] == closed["post_run_fingerprint"]
        (repo / ".fake-mode").write_text("fix\n", encoding="utf-8")
        done = _run(repo, env, "execute")
        assert done.returncode == 0, done.stderr + done.stdout
        assert (repo / "app.py").read_text() == "value = 2\n"
        assert (repo / "scratch.txt").read_text() == "uncommitted executor file\n"
        assert _workflow(repo)["rounds_used"] == 1
        assert parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
        # The predecessor's counters were not reset.
        closed_again = json.loads((repo / ".handoff-logs" / "closed-workflows" / f"{parent}.json").read_text())
        assert closed_again["rounds_used"] == 3


def _launch_marker(tmp_path: Path) -> tuple[Path, Path]:
    """Harmless legacy 'claude' that only records that it was launched."""
    marker = tmp_path / "legacy-launches"
    stub = tmp_path / "legacy-marker-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"echo launched >> {json.dumps(str(marker))}\n"
        "echo '{\"type\":\"result\",\"is_error\":false,\"usage\":{}}'\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub, marker


def _legacy_launches(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.is_file() else 0


@pytest.mark.parametrize(
    ("switch", "ending"),
    [("legacy", "cancel"), ("removed", "cancel"), ("legacy", "fail"), ("removed", "fail"), ("legacy", "complete")],
)
def test_watch_after_transport_switch_keeps_dispatch_hold(tmp_path: Path, switch: str, ending: str) -> None:
    import signal

    mode, sleep_s = {"cancel": ("early_ready", 25), "fail": ("early_fail", 4), "complete": ("early_ready", 4)}[ending]
    repo, config, env = _setup_mode(tmp_path, mode, sleep_s=sleep_s, wait=1.0)
    stub, marker = _launch_marker(tmp_path)
    env = {**env, "HANDOFF_CLAUDE_BIN": str(stub)}
    outstanding = repo / ".handoff-logs" / "outstanding.json"
    with RunningServer(config):
        _run(repo, env, "approve", check=True)
        assert _run(repo, env, "execute").returncode == 2
        _wait_for_worker(repo)
        if switch == "legacy":
            (repo / ".handoff-config.json").write_text('{"transport": "legacy"}\n', encoding="utf-8")
        else:
            (repo / ".handoff-config.json").unlink()
        watch = subprocess.Popen(
            [str(HANDOFF_BIN), "watch", str(repo)],
            env={**env, "HANDOFF_POLL_INTERVAL": "1", "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            time.sleep(1.5)
            if ending == "cancel":
                canceled = _run(repo, env, "cancel")
                assert '"outcome": "canceled"' in canceled.stdout, canceled.stdout + canceled.stderr
            deadline = time.time() + 30
            while time.time() < deadline and outstanding.exists():
                time.sleep(0.2)
            assert not outstanding.exists()
            time.sleep(3.5)  # several more legacy polls
        finally:
            os.killpg(watch.pid, signal.SIGTERM)
            out, _ = watch.communicate(timeout=10)
        assert _launches(repo) == 1
        assert _legacy_launches(marker) == 0, out
        status = parse_handoff((repo / "HANDOFF.md").read_text()).status
        if ending == "complete":
            assert status == "READY FOR QA"
            assert out.count("NOTIFY: Handoff: READY FOR QA") == 1, out
            assert _workflow(repo)["dispatch_hold"] is False
            return
        assert status == "READY FOR EXECUTION"
        assert "watch will not retry" in out, out
        assert _workflow(repo)["dispatch_hold"] is True
        assert _workflow(repo)["rounds_used"] == 1

        # Only an explicit, reviewed execute proceeds, and it clears the hold.
        explicit = _run(repo, env, "execute")
        assert explicit.returncode == 0, explicit.stderr + explicit.stdout
        assert "cleared the A2A dispatch hold" in explicit.stdout
        assert _legacy_launches(marker) == 1
        assert _workflow(repo)["dispatch_hold"] is False


def test_plain_legacy_watch_dispatches_without_python(tmp_path: Path) -> None:
    import signal

    repo = make_repo(tmp_path)
    write_handoff(repo, status="READY FOR EXECUTION")
    stub, marker = _launch_marker(tmp_path)
    env = os.environ.copy()
    env.update({"HANDOFF_CLAUDE_BIN": str(stub), "HANDOFF_A2A_BIN": str(tmp_path / "no-a2a"), "HANDOFF_POLL_INTERVAL": "1"})
    watch = subprocess.Popen(
        [str(HANDOFF_BIN), "watch", str(repo)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not marker.exists():
            time.sleep(0.2)
    finally:
        os.killpg(watch.pid, signal.SIGTERM)
        out, _ = watch.communicate(timeout=10)
    assert _legacy_launches(marker) == 1, out
    assert "no-a2a" not in out
