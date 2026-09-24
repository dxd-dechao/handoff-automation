"""`handoff model`: switching the Executor within a workflow (fake workers).

Binds loopback ports and starts managed servers; see test_a2a_service.py.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from a2a_harness import HANDOFF_BIN, field, git, handoff, managed_repo, stop_managed, write_handoff
from handoff_a2a.workspace import approved_plan_hash, parse_handoff


@pytest.fixture
def cleanup():
    repos: list[Path] = []
    yield repos.append
    for repo in repos:
        stop_managed(repo)


def _workflow(repo: Path) -> dict:
    return json.loads((repo / ".handoff-logs" / "workflow.json").read_text())


def _manifests(repo: Path) -> list[dict]:
    items = [json.loads(p.read_text()) for p in (repo / ".handoff-logs").glob("*-manifest.json")]
    return sorted(items, key=lambda m: m.get("started_at") or "")


def _server(repo: Path) -> dict:
    return json.loads((repo / ".handoff-logs" / "server.json").read_text())


def _history(repo: Path) -> list[dict]:
    path = repo / ".handoff-logs" / "service" / "history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def _correction(repo: Path, qa: str = "app.py must set value = 2, not 1.") -> None:
    write_handoff(repo, status="CHANGES REQUESTED", notes="implemented value=1", qa=qa)
    (repo / ".fake-mode").write_text("fix\n", encoding="utf-8")


def _started(repo: Path, env: dict[str, str]) -> None:
    result = handoff(env, "server", "start", str(repo))
    assert result.returncode == 0, result.stdout + result.stderr


def test_immediate_same_and_cross_provider_switch_keep_workflow_state(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    workflow = _workflow(repo)
    plan_hash = workflow["approved_plan_hash"]
    assert handoff(env, "execute", str(repo)).returncode == 0

    _correction(repo)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    before = _workflow(repo)
    switched = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert switched.returncode == 0, switched.stdout + switched.stderr
    assert "cursor / other-model (generation 2) (verified)" in switched.stdout
    after = _workflow(repo)
    for key in ("workflow_id", "approved_plan_hash", "rounds_used", "dispatch_hold", "post_run_fingerprint", "branch"):
        assert after[key] == before[key], key
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert approved_plan_hash((repo / "HANDOFF.md").read_text()) == plan_hash
    second = handoff(env, "execute", str(repo))
    assert second.returncode == 0, second.stdout + second.stderr
    assert (repo / "app.py").read_text() == "value = 2\n"

    _correction(repo, qa="Revert app.py to value = 1 (the fake worker's default change).")
    (repo / ".fake-mode").write_text("success\n", encoding="utf-8")
    cross = handoff(env, "model", str(repo), "--provider", "codex", "--model", "fake-model")
    assert cross.returncode == 0, cross.stdout + cross.stderr
    third = handoff(env, "execute", str(repo))
    assert third.returncode == 0, third.stdout + third.stderr
    manifests = _manifests(repo)
    assert [(m["executor"]["provider"], m["executor"]["model"], m["executor"]["config_generation"]) for m in manifests] == [
        ("cursor", "fake-model", 1),
        ("cursor", "other-model", 2),
        ("codex", "fake-model", 3),
    ]
    assert {m["workflow_id"] for m in manifests} == {workflow["workflow_id"]}
    assert _workflow(repo)["rounds_used"] == 3
    assert _workflow(repo)["approved_plan_hash"] == plan_hash
    shown = handoff(env, "model", str(repo))
    assert "codex / fake-model (generation 3" in field(shown.stdout, "selected")


def test_immediate_switch_refused_while_a_run_is_outstanding(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    (repo / ".fake-mode").write_text("early_ready\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("20\n", encoding="utf-8")
    config = json.loads((repo / ".handoff-config.json").read_text())
    config["a2a"]["wait_timeout_s"] = 1
    (repo / ".handoff-config.json").write_text(json.dumps(config))
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    assert handoff(env, "execute", str(repo)).returncode == 2  # still working
    refused = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert refused.returncode == 2
    assert "cannot switch now" in refused.stderr and "--after-current" in refused.stderr
    assert _server(repo)["config_generation"] == 1
    shown = handoff(env, "model", str(repo))
    assert "current run: cursor / fake-model (generation 1" in shown.stdout
    canceled = handoff(env, "cancel", str(repo))
    assert '"outcome": "canceled"' in canceled.stdout, canceled.stdout + canceled.stderr


def _watch(repo: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(HANDOFF_BIN), "watch", str(repo)],
        env={**env, "HANDOFF_POLL_INTERVAL": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen[str]) -> str:
    os.killpg(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=15)
    return out


def _wait(predicate, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def test_after_current_applies_once_through_a_running_watch(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    (repo / ".fake-sleep").write_text("5\n", encoding="utf-8")
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    watch = _watch(repo, env)
    try:
        assert _wait(lambda: (repo / "WORKER_STARTED").exists())
        queued = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model", "--after-current")
        assert queued.returncode == 0, queued.stderr
        assert "current run keeps cursor / fake-model" in queued.stdout
        again = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model", "--after-current")
        assert "already queued" in again.stdout
        assert _server(repo)["config_generation"] == 1  # live service untouched
        assert _wait(lambda: parse_handoff((repo / "HANDOFF.md").read_text()).status == "READY FOR QA"
                     and not (repo / ".handoff-logs" / "outstanding.json").exists())
        assert _wait(lambda: _server(repo)["config_generation"] == 2)
        (repo / ".fake-sleep").write_text("0\n", encoding="utf-8")
        _correction(repo)
        assert _wait(lambda: len(_manifests(repo)) == 2 and _manifests(repo)[-1].get("outcome") == "completed")
        time.sleep(2.5)
    finally:
        out = _stop(watch)
    manifests = _manifests(repo)
    assert [(m["executor"]["model"], m["executor"]["config_generation"]) for m in manifests] == [
        ("fake-model", 1),
        ("other-model", 2),
    ], out
    assert [e["event"] for e in _history(repo)].count("pending_applied") == 1
    assert not (repo / ".handoff-logs" / "service" / "pending-selection.json").exists()
    assert int((repo / "WORKER_LAUNCHES").read_text()) == 2
    assert out.count("applied queued Executor selection") == 1, out


def test_queued_change_after_cancel_keeps_hold_and_waits_for_unknown_ack(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    (repo / ".fake-mode").write_text("early_ready\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("20\n", encoding="utf-8")
    config = json.loads((repo / ".handoff-config.json").read_text())
    config["a2a"]["wait_timeout_s"] = 1
    (repo / ".handoff-config.json").write_text(json.dumps(config))
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    assert handoff(env, "execute", str(repo)).returncode == 2
    assert handoff(env, "model", str(repo), "--provider", "codex", "--model", "fake-model", "--after-current").returncode == 0
    assert '"outcome": "canceled"' in handoff(env, "cancel", str(repo)).stdout
    assert _workflow(repo)["dispatch_hold"] is True
    watch = _watch(repo, env)
    try:
        assert _wait(lambda: _server(repo)["config_generation"] == 2)
        time.sleep(3)
    finally:
        out = _stop(watch)
    assert "watch will not retry" in out or "held" in out, out
    assert _workflow(repo)["dispatch_hold"] is True  # applying never clears the hold
    assert _workflow(repo)["rounds_used"] == 1
    assert int((repo / "WORKER_LAUNCHES").read_text()) == 1
    assert "codex / fake-model (generation 2)" in handoff(env, "model", str(repo)).stdout

    # Unknown acknowledgement: a queued change stays queued while ownership is uncertain.
    (repo / ".handoff-logs" / "outstanding.json").write_text(json.dumps({"run_record": "missing", "execution_id": "x"}))
    assert handoff(env, "model", str(repo), "--provider", "cursor", "--model", "fake-model", "--after-current").returncode == 0
    from handoff_a2a.selection import apply_pending

    assert apply_pending(repo, emit=lambda _m: None) == "deferred"
    assert (repo / ".handoff-logs" / "service" / "pending-selection.json").is_file()
    (repo / ".handoff-logs" / "outstanding.json").unlink()


def test_failed_switch_restores_previous_service_and_failed_queue_blocks_dispatch(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    previous = _server(repo)
    failing = {**env, "HANDOFF_A2A_REFUSE_GENERATION": "2"}
    result = handoff(failing, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert result.returncode == 1
    assert "restored the previous selection and restarted it" in result.stderr, result.stderr
    assert _server(repo) == previous
    assert "cursor / fake-model (generation 1) (service verified)" in handoff(env, "model", str(repo)).stdout
    assert "switch_failed" in [e["event"] for e in _history(repo)]

    queued = handoff(failing, "model", str(repo), "--provider", "cursor", "--model", "other-model", "--after-current")
    assert queued.returncode == 0
    blocked = handoff(failing, "execute", str(repo))
    assert blocked.returncode != 0 and "failed" in blocked.stderr
    assert _workflow(repo)["rounds_used"] == 0
    assert not (repo / "WORKER_LAUNCHES").exists()
    again = handoff(env, "execute", str(repo))
    assert again.returncode != 0 and "cancel-pending" in again.stderr  # failure kept until the human acts
    assert handoff(env, "model", str(repo), "--cancel-pending").returncode == 0
    done = handoff(env, "execute", str(repo))
    assert done.returncode == 0, done.stdout + done.stderr
    assert _manifests(repo)[0]["executor"]["model"] == "fake-model"


def test_switch_vs_submission_locks_and_stale_generation(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    write_handoff(repo, status="DRAFT")
    _started(repo, env)
    assert handoff(env, "approve", str(repo)).returncode == 0
    submit = repo / ".handoff-logs" / "submit.lock"
    submit.mkdir()
    busy = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert busy.returncode == 2 and "busy" in busy.stderr
    held = handoff(env, "execute", str(repo))
    assert held.returncode != 0 and "in progress" in held.stderr
    submit.rmdir()
    assert _server(repo)["config_generation"] == 1

    # A selection published without its service (e.g. edited by hand) is never dispatched to.
    server = _server(repo)
    server["config_generation"] = 7
    (repo / ".handoff-logs" / "server.json").write_text(json.dumps(server))
    stale = handoff(env, "execute", str(repo))
    assert stale.returncode != 0 and "not running the selected Executor" in stale.stderr
    assert not (repo / ".handoff-logs" / "outstanding.json").exists()
    assert _workflow(repo)["rounds_used"] == 0 and _workflow(repo)["reserved_execution_id"] is None

    # Submission in flight (outstanding written under the lock): switching refuses.
    server["config_generation"] = 1
    (repo / ".handoff-logs" / "server.json").write_text(json.dumps(server))
    hold = tmp_path / "hold"
    hold.write_text("1")
    proc = subprocess.Popen(
        [str(HANDOFF_BIN), "execute", str(repo)],
        env={**env, "HANDOFF_A2A_HOLD_AFTER_RECORD": str(hold)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait(lambda: (repo / ".handoff-logs" / "outstanding.json").exists(), 15)
        refused = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
        assert refused.returncode == 2 and "outstanding" in refused.stderr
    finally:
        hold.unlink()
        out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, out + err
    assert int((repo / "WORKER_LAUNCHES").read_text()) == 1


def test_plan_constraint_and_unmanaged_endpoint_refuse(tmp_path: Path, cleanup) -> None:
    repo, env = managed_repo(tmp_path)
    cleanup(repo)
    text = (repo / "HANDOFF.md").read_text()
    write_handoff(repo, status="DRAFT")
    constrained = (repo / "HANDOFF.md").read_text().replace(
        "**Branch:** main", "**Branch:** main\n\n**Executor:** cursor / fake-model"
    )
    (repo / "HANDOFF.md").write_text(constrained)
    refused = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert refused.returncode == 2 and "re-approved plan" in refused.stderr
    assert _server(repo)["config_generation"] == 1
    (repo / "HANDOFF.md").write_text(text)
    (repo / ".handoff-config.json").write_text('{"transport": "legacy"}\n')
    unmanaged = handoff(env, "model", str(repo), "--provider", "cursor", "--model", "other-model")
    assert unmanaged.returncode == 2 and "manually configured endpoint" in unmanaged.stderr
