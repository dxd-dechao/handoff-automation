from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from a2a_harness import (
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


def test_approve_execute_correction_and_dirty_refusals(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _ignore_probes(repo)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
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
        for path in (repo / ".handoff-logs").glob("*-manifest.json"):
            body = path.read_text(encoding="utf-8")
            assert "test-token" not in body
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

