"""Preflight, sandbox probes, skill copies, and delivery boundaries."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from a2a_harness import (
    coding_payload,
    git,
    handoff,
    ignore_probes,
    make_repo,
    managed_env,
    managed_repo,
    stop_managed,
    write_handoff,
)
from handoff_a2a.processes import LIVENESS_DEAD, LIVENESS_UNKNOWN, process_liveness
from handoff_a2a.providers import failure_kind
from handoff_a2a.service import PROBE_NOT_PERMITTED, ServiceState
from handoff_a2a.workflow import load_workflow, save_workflow
from handoff_a2a.workspace import GitWorkspace, approved_plan_hash, parse_handoff


def _json(result) -> dict:
    return json.loads(result.stdout)


@pytest.mark.server
def test_preflight_lists_every_blocker(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="READY FOR EXECUTION", branch="feature")
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "checkout", "-q", "main")
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    assert handoff(env, "approve", str(repo)).returncode == 0
    (repo / "HANDOFF.md").write_text(
        (repo / "HANDOFF.md").read_text().replace("### Goal", "### Goal\n\nchanged"),
        encoding="utf-8",
    )
    logs = repo / ".handoff-logs"
    (logs / "outstanding.json").write_text('{"execution_id": "x"}\n', encoding="utf-8")
    blocked = handoff(env, "preflight", str(repo), "--json")
    body = _json(blocked)
    assert blocked.returncode == 1
    codes = {item["code"] for item in body["blockers"]}
    assert {"branch", "baseline", "plan_hash", "outstanding", "service_stopped"} <= codes
    assert any(item["fix"].startswith("git switch") for item in body["blockers"])
    assert "dirty.txt" in body["dirty_paths"]
    assert "HANDOFF.md" not in body["dirty_paths"]
    refused = handoff(env, "execute", str(repo))
    assert refused.returncode == 1
    assert "dirty.txt" in refused.stderr and "git switch feature" in refused.stderr
    assert "outstanding" in refused.stderr


@pytest.mark.server
def test_first_execute_creates_a_missing_branch(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="feature")
    assert handoff(env, "approve", str(repo)).returncode == 0
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    pre = handoff(env, "preflight", str(repo), "--json")
    body = _json(pre)
    assert pre.returncode == 0, pre.stderr
    assert body["ready"] is True and body["notes"]
    run = handoff(env, "execute", str(repo))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "created branch feature" in run.stdout
    assert git(repo, "branch", "--show-current").stdout.strip() == "feature"
    manifests = list((repo / ".handoff-logs").glob("*-manifest.json"))
    assert manifests and json.loads(manifests[0].read_text())["branch_created"] == "feature"


@pytest.mark.server
def test_bytecode_baseline_and_reapprove_rebaseline(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    empty = tmp_path / "empty-exclude"
    empty.write_text("", encoding="utf-8")
    git(repo, "config", "core.excludesFile", str(empty))
    write_handoff(repo, status="DRAFT")
    (repo / ".fake-mode").write_text("bytecode\n", encoding="utf-8")
    approved = handoff(env, "approve", str(repo))
    assert approved.returncode == 0, approved.stderr
    assert "rebaselined" not in approved.stdout
    workflow_id = load_workflow(repo)["workflow_id"]
    assert load_workflow(repo)["post_run_fingerprint"] is None
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    try:
        run = handoff(env, "execute", str(repo))
        assert run.returncode == 0, run.stdout + run.stderr
        assert (repo / "pkg" / "__pycache__" / "m.cpython-312.pyc").is_file()
        assert (repo / "x.pyc").is_file()
        (repo / "pkg" / "__pycache__" / "m.cpython-312.pyc").unlink()
        (repo / "x.pyc").unlink()
        write_handoff(repo, status="CHANGES REQUESTED", notes="left bytecode")
        ready = handoff(env, "preflight", str(repo), "--json")
        assert ready.returncode == 0, ready.stdout + ready.stderr
        assert _json(ready)["ready"] is True
        rounds = load_workflow(repo)["rounds_used"]
        assert rounds == 1
        legacy = load_workflow(repo)
        legacy["post_run_fingerprint"] = "f" * 64
        save_workflow(repo, legacy)
        blocked = handoff(env, "preflight", str(repo), "--json")
        body = _json(blocked)
        fingerprint = next(item for item in body["blockers"] if item["code"] == "fingerprint")
        assert fingerprint["message"] == "workspace code does not match the last recorded post-run snapshot"
        assert "re-approve" in fingerprint["fix"]
        text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
        (repo / "HANDOFF.md").write_text(
            text.replace("Set app.py value according to the current round.", "Revised remaining work."),
            encoding="utf-8",
        )
        again = handoff(env, "approve", str(repo))
        assert again.returncode == 0, again.stderr
        assert "rebaselined code snapshot (previous ffffffffffff)" in again.stdout
        saved = load_workflow(repo)
        assert saved["workflow_id"] == workflow_id
        assert saved["rounds_used"] == rounds
        assert saved["previous_post_run_fingerprint"] == "f" * 64
        assert saved["rebaselined_at"]
        assert saved["post_run_fingerprint"] != "f" * 64
        passed = handoff(env, "preflight", str(repo), "--json")
        assert passed.returncode == 0, passed.stdout + passed.stderr
        assert _json(passed)["ready"] is True
        unchanged = handoff(env, "approve", str(repo))
        assert unchanged.returncode == 0, unchanged.stderr
        assert "rebaselined" not in unchanged.stdout
        assert load_workflow(repo)["post_run_fingerprint"] == saved["post_run_fingerprint"]
    finally:
        stop_managed(repo)


def test_revised_approve_refuses_a_dirty_tree(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    assert handoff(env, "approve", str(repo)).returncode == 0
    workflow = load_workflow(repo)
    workflow["post_run_fingerprint"] = "a" * 64
    workflow["post_run_branch"] = "main"
    workflow["rounds_used"] = 1
    save_workflow(repo, workflow)
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    (repo / "HANDOFF.md").write_text(
        text.replace("Set app.py value according to the current round.", "Revised remaining work."),
        encoding="utf-8",
    )
    (repo / "app.py").write_text("value = 4\n", encoding="utf-8")
    before_status = parse_handoff((repo / "HANDOFF.md").read_text()).status
    before = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    refused = handoff(env, "approve", str(repo))
    assert refused.returncode != 0
    assert "app.py" in refused.stderr
    assert "commit or discard" in refused.stderr
    assert parse_handoff((repo / "HANDOFF.md").read_text()).status == before_status
    after = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    assert after["approved_plan_hash"] == before["approved_plan_hash"]
    assert after["post_run_fingerprint"] == "a" * 64
    assert "rebaselined_at" not in after


def test_existing_branch_is_not_switched_and_dirty_tree_is_untouched(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "checkout", "-q", "main")
    (repo / "dirty.txt").write_text("keep\n", encoding="utf-8")
    write_handoff(repo, status="DRAFT", branch="feature")
    assert handoff(env, "approve", str(repo)).returncode == 0
    refused = handoff(env, "execute", str(repo))
    assert refused.returncode != 0
    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert (repo / "dirty.txt").read_text() == "keep\n"


def test_legacy_preflight_refuses_without_python(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    env = os.environ.copy()
    env["HANDOFF_A2A_BIN"] = str(tmp_path / "missing-a2a")
    result = handoff(env, "preflight", str(repo))
    assert result.returncode == 1 and "preflight needs managed A2A" in result.stderr
    as_json = handoff(env, "preflight", str(repo), "--json")
    body = _json(as_json)
    assert body["error"] == "preflight needs managed A2A" and body["kind"] == "preflight"


def test_permission_error_has_no_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from handoff_a2a.__main__ import main

    def boom(_argv):
        raise PermissionError(errno.EPERM, "Operation not permitted", "/tmp/sandbox")

    monkeypatch.setattr("handoff_a2a.setup.main", boom)
    code = main(["setup", "--json", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    body = json.loads(captured.out)
    assert "/tmp/sandbox" in body["error"] and "permission denied; sandbox?" in body["error"]


def test_network_failure_is_not_a_login_problem() -> None:
    assert failure_kind("read ECONNRESET") == "network"
    assert failure_kind("Authentication required") == "auth"
    assert failure_kind("Not logged in") == "auth"


def test_ps_denied_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("handoff_a2a.processes.os.kill", lambda *_a, **_k: None)
    monkeypatch.setattr("handoff_a2a.processes.os.stat", lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.ENOENT, "no proc")))

    def denied(*_a, **_k):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("handoff_a2a.processes.subprocess.run", denied)
    assert process_liveness(123, "123:real-start") == LIVENESS_UNKNOWN

    def gone(*_a, **_k):
        raise ProcessLookupError

    monkeypatch.setattr("handoff_a2a.processes.os.kill", gone)
    assert process_liveness(123, "123:real-start") == LIVENESS_DEAD


def test_section_boundary_note_and_diff(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    before = parse_handoff(text)
    gitws = GitWorkspace("local", repo)
    stripped = text.replace("\n---\n\n## Execution Notes", "\n## Execution Notes")
    (repo / "HANDOFF.md").write_text(stripped.replace("**Status:** READY FOR EXECUTION", "**Status:** READY FOR QA"), encoding="utf-8")
    _after, ok, reason, note = gitws.evaluate_transition(before)
    assert ok and reason is None and note
    changed = stripped.replace("### Goal", "### Goal\n\nsecret change")
    (repo / "HANDOFF.md").write_text(changed.replace("**Status:** READY FOR EXECUTION", "**Status:** READY FOR QA"), encoding="utf-8")
    _after, ok, reason, note = gitws.evaluate_transition(before)
    assert not ok and note is None and "### Goal" in (reason or "") and "secret change" in (reason or "")


def test_transition_rejects_bad_headings(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    text = write_handoff(repo, status="READY FOR EXECUTION")
    before = parse_handoff(text)
    gitws = GitWorkspace("local", repo)

    def deliver(updated: str) -> str:
        (repo / "HANDOFF.md").write_text(updated, encoding="utf-8")
        _after, ok, reason, _note = gitws.evaluate_transition(before)
        assert not ok
        return reason or ""

    assert "2 times" in deliver(text + "\n## QA Feedback\n\nextra\n") and "QA Feedback" in deliver(
        text + "\n## QA Feedback\n\nextra\n"
    )
    assert "Current Task" in deliver(text.replace("## Execution Notes", "## Current Task\n\nextra\n\n## Execution Notes", 1))
    assert "Execution Notes" in deliver(text.replace("## Execution Notes\n\nNot started.\n\n---\n\n", ""))
    swapped = text.replace(
        "## Execution Notes\n\nNot started.\n\n---\n\n## QA Feedback\n\nNot run.\n",
        "## QA Feedback\n\nNot run.\n\n## Execution Notes\n\nNot started.\n",
    )
    assert "before" in deliver(swapped)
    quoted = text.replace("### Goal\n", "### Goal\n\nQuoted `## QA Feedback` stays inline.\n")
    (repo / "HANDOFF.md").write_text(quoted, encoding="utf-8")
    before = parse_handoff(quoted)
    ready = quoted.replace("**Status:** READY FOR EXECUTION", "**Status:** READY FOR QA")
    (repo / "HANDOFF.md").write_text(ready, encoding="utf-8")
    _after, ok, reason, _note = gitws.evaluate_transition(before)
    assert ok and reason is None
    (repo / "HANDOFF.md").write_bytes(ready.replace("\n", "\r\n").encode("utf-8"))
    _after, ok, reason, _note = gitws.evaluate_transition(before)
    assert ok and reason is None


def test_approved_plan_hash_pin() -> None:
    text = "## Current Task\n\n**Status:** READY\n\nplan body\n"
    # Pinned so boundary normalization cannot change the approval hash.
    assert approved_plan_hash(text) == "99f8cd04e18a04b4c2268d9804b1fc387cf3dc41923493896f7ca35c7b76ce9d"


def test_skill_elsewhere_is_reported_and_not_modified(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    home = Path(env["HOME"])
    copy = home / ".claude" / "skills" / "_handoff-cli__skills__handoff-cli"
    source = Path("skills/handoff-cli")
    copy.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(source, copy)
    before = (copy / "SKILL.md").read_bytes()
    other = home / ".claude" / "skills" / "unrelated"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: other\n---\n", encoding="utf-8")
    status = handoff(env, "skill", "status", str(repo), "--json")
    body = _json(status)
    claude = next(item for item in body["locations"] if item["host"] == "claude" and item["scope"] == "user")
    assert claude["state"] == "elsewhere"
    assert claude["found_path"] == str(copy)
    assert claude["current_content"] is True
    assert claude["stale"] is False
    assert (copy / "SKILL.md").read_bytes() == before
    init = handoff(env, "init", str(repo), "--planner", "claude")
    assert init.returncode == 0, init.stderr
    assert str(copy) in init.stdout
    assert "stale (content differs" not in init.stdout
    assert not (repo / ".claude" / "skills" / "handoff-cli").exists()


def _json_out(text: str) -> dict:
    return json.loads(text)


def _approve(tmp_path: Path, *, branch: str = "main") -> tuple[Path, dict[str, str]]:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch=branch)
    assert handoff(env, "approve", str(repo)).returncode == 0
    return repo, env


def _watch_for(repo: Path, env: dict[str, str], timeout: float = 3.0) -> str:
    proc = subprocess.Popen(
        [str(Path(__file__).resolve().parents[1] / "bin" / "handoff"), "watch", str(repo)],
        env={**env, "HANDOFF_POLL_INTERVAL": "1", "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(timeout)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    return out


def _manifest(repo: Path) -> dict:
    return json.loads(next((repo / ".handoff-logs").glob("*-manifest.json")).read_text(encoding="utf-8"))


def _running_state(managed) -> ServiceState:
    return ServiceState(
        running=True,
        verified=True,
        port=managed.port,
        generation=managed.generation,
        provider="cursor",
        model="fake-model",
        probe="ok",
        liveness="alive",
    )


def _unknown_state(managed) -> ServiceState:
    return ServiceState(
        running=False,
        verified=False,
        port=managed.port,
        generation=managed.generation,
        provider="cursor",
        model="fake-model",
        probe=PROBE_NOT_PERMITTED,
        liveness=LIVENESS_UNKNOWN,
        record={"pid": os.getpid(), "started_at": "then", "log": "kept"},
    )


@pytest.mark.server
def test_watch_retries_a_stopped_service_and_refuses_other_blockers(tmp_path: Path) -> None:
    (tmp_path / "stopped").mkdir()
    (tmp_path / "dirty").mkdir()
    repo, env = _approve(tmp_path / "stopped")
    out = _watch_for(repo, env)
    assert "dispatch deferred" in out and "watch will retry at the next poll" in out, out
    assert "execute failed" not in out
    refused = handoff(env, "execute", str(repo))
    assert refused.returncode == 1
    assert "handoff server start" in refused.stderr and "managed service is stopped" in refused.stderr

    dirty, dirty_env = _approve(tmp_path / "dirty")
    (dirty / "dirty.txt").write_text("x\n", encoding="utf-8")
    held = _watch_for(dirty, dirty_env)
    assert "execute failed" in held and "watch will retry" not in held, held


def test_watch_retries_an_unknown_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _env = _approve(tmp_path)
    monkeypatch.setattr("handoff_a2a.integration.inspect_service", _unknown_state)
    sleeps = {"n": 0}

    def nap(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("handoff_a2a.integration.time.sleep", nap)
    from handoff_a2a.integration import cmd_watch_async

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(cmd_watch_async(repo, 1))
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "dispatch deferred" in out and "watch will retry at the next poll" in out, out
    assert "probe not permitted" in out


def test_status_lists_stale_planner_skills(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="main")
    empty = _json(handoff(env, "status", str(repo), "--json"))
    assert empty["schema"] == "urn:handoff-automation:cli-output:v1" and empty["kind"] == "status"
    assert empty["planner_skill"] == {"stale": []}
    assert "status" in empty and "execution" in empty and "preflight" in empty
    assert not any(line.startswith("skill:") for line in handoff(env, "status", str(repo)).stdout.splitlines())

    copy = Path(env["HOME"]) / ".cursor" / "skills" / "_handoff-cli__skills__handoff-cli"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nold\n", encoding="utf-8")
    shown = _json(handoff(env, "status", str(repo), "--json"))
    assert shown["planner_skill"]["stale"] == [str(copy)]
    assert shown["status"] == empty["status"] and shown["preflight"] == empty["preflight"]
    human = handoff(env, "status", str(repo))
    skill_lines = [line for line in human.stdout.splitlines() if line.startswith("skill:")]
    assert skill_lines == [
        f"skill:  {copy}: stale (content differs from this checkout's skills/handoff-cli); "
        "update it with the tool that installed it (e.g. skillshare); handoff never modifies it"
    ]


def test_preflight_output_ignores_a_stale_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="READY FOR EXECUTION", branch="main")
    assert handoff(env, "approve", str(repo)).returncode == 0
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setattr("handoff_a2a.integration.inspect_service", lambda managed: _unknown_state(managed))
    from handoff_a2a.integration import cmd_preflight

    assert cmd_preflight(repo, as_json=False) == 1
    first = capsys.readouterr()
    assert cmd_preflight(repo, as_json=True) == 1
    first_json = capsys.readouterr().out
    copy = Path(env["HOME"]) / ".cursor" / "skills" / "_handoff-cli__skills__handoff-cli"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: handoff-cli\ndescription: skillshare\n---\nold\n", encoding="utf-8")
    assert cmd_preflight(repo, as_json=False) == 1
    second = capsys.readouterr()
    assert cmd_preflight(repo, as_json=True) == 1
    second_json = capsys.readouterr().out
    assert second.out == first.out and second.err == first.err and second_json == first_json
    assert "planner_skill" not in first_json and "skill:" not in first.out


def test_preflight_reports_service_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _env = _approve(tmp_path)
    monkeypatch.setattr("handoff_a2a.integration.inspect_service", _unknown_state)
    from handoff_a2a.integration import cmd_preflight, cmd_status_async

    assert cmd_preflight(repo, as_json=True) == 1
    pre = _json_out(capsys.readouterr().out)
    unknown = next(item for item in pre["blockers"] if item["code"] == "service_unknown")
    assert unknown["fix"] and "sandbox" in unknown["message"]
    assert asyncio.run(cmd_status_async(repo, as_json=True)) == 0
    status = _json_out(capsys.readouterr().out)
    assert status["preflight"]["ready"] is False
    assert any(item["code"] == "service_unknown" for item in status["preflight"]["blockers"])


def test_unknown_service_refuses_start_stop_and_defers_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _env = _approve(tmp_path)
    record_path = repo / ".handoff-logs" / "service" / "process.json"
    record_path.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpid(), "start_identity": "kept"}) + "\n", encoding="utf-8")
    monkeypatch.setattr("handoff_a2a.service.record_liveness", lambda *_a, **_k: LIVENESS_UNKNOWN)
    monkeypatch.setattr("handoff_a2a.service.port_probe", lambda *_a, **_k: PROBE_NOT_PERMITTED)
    from handoff_a2a.integration import DispatchDeferred, cmd_execute_async
    from handoff_a2a.service import ServiceError, cmd_start, cmd_status, cmd_stop

    assert cmd_status(repo, as_json=True) == 2
    body = _json_out(capsys.readouterr().out)
    assert body["state"] == "unknown" and body["probe"] == "not_permitted"
    assert record_path.is_file() and "kept" in record_path.read_text(encoding="utf-8")
    with pytest.raises(ServiceError, match="refusing to start"):
        cmd_start(repo, None)
    with pytest.raises(ServiceError, match="refusing to signal"):
        cmd_stop(repo)
    with pytest.raises(DispatchDeferred, match="probe not permitted"):
        asyncio.run(cmd_execute_async(repo))


def test_init_planner_permission_error_warns_and_keeps_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    ignore_probes(repo)
    (repo / "HANDOFF.md").unlink()
    marker = "HANDOFF stays\n"
    (repo / "HANDOFF.md").write_text(marker, encoding="utf-8")
    for key, value in managed_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    def boom(location, journal=None):
        journal.mkdir(location.path, 0o755)
        partial = location.path / "SKILL.md"
        journal.before_write(partial)
        partial.write_text("partial\n", encoding="utf-8")
        raise PermissionError(errno.EPERM, "Operation not permitted", str(partial))

    monkeypatch.setattr("handoff_a2a.setup.install_skill", boom)
    from handoff_a2a.setup import main

    template = Path(__file__).resolve().parents[1] / "templates" / "HANDOFF.md"
    code = main([str(repo), "--template", str(template), "--executor", "cursor", "--model", "fake-model", "--planner", "claude"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "warning:" in captured.out and "handoff skill install" in captured.out
    assert (repo / "HANDOFF.md").read_text(encoding="utf-8") == marker
    assert (repo / ".handoff-config.json").is_file()
    assert not (repo / ".claude" / "skills" / "handoff-cli" / "SKILL.md").exists()


def test_init_omits_install_hint_when_a_copy_exists(tmp_path: Path) -> None:
    (tmp_path / "none").mkdir()
    (tmp_path / "else").mkdir()
    none_repo, none_env = managed_repo(tmp_path / "none")
    missing = handoff(none_env, "init", str(none_repo))
    assert missing.returncode == 0, missing.stderr
    assert "handoff skill install" in missing.stdout

    repo, env = managed_repo(tmp_path / "else")
    home = Path(env["HOME"])
    copy = home / ".claude" / "skills" / "_handoff-cli__skills__handoff-cli"
    shutil.copytree(Path("skills/handoff-cli"), copy)
    present = handoff(env, "init", str(repo))
    assert present.returncode == 0, present.stderr
    assert "handoff skill install" not in present.stdout
    assert str(copy) in present.stdout


def test_models_json_distinguishes_network_from_login(tmp_path: Path) -> None:
    script = tmp_path / "cursor-agent"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('fake 1.0')\n"
        "    raise SystemExit(0)\n"
        "kind = os.environ.get('FAKE_CURSOR_KIND', 'network')\n"
        "print('read ECONNRESET' if kind == 'network' else 'Authentication required', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = managed_env(tmp_path)
    env["HANDOFF_CURSOR_BIN"] = str(script)
    network = handoff({**env, "FAKE_CURSOR_KIND": "network"}, "models", "--provider", "cursor", "--json")
    body = _json(network)
    assert network.returncode == 1
    assert body["error_kind"] == "network" and "login_command" not in body
    assert "not a login problem" in body["error"]
    auth = handoff({**env, "FAKE_CURSOR_KIND": "auth"}, "models", "--provider", "cursor", "--json")
    denied = _json(auth)
    assert denied["error_kind"] == "auth" and denied["login_command"] == "cursor-agent login"


def test_eperm_connect_is_not_a_stopped_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _env = _approve(tmp_path)
    monkeypatch.setattr("handoff_a2a.integration.inspect_service", _running_state)

    async def denied(self):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("handoff_a2a.client.CodingClient.connect", denied)
    from handoff_a2a.integration import main

    code = main(["--repo", str(repo), "execute"])
    err = capsys.readouterr().err
    assert code == 1
    assert "connection not permitted (sandbox?)" in err and "server start" not in err

    token = repo / ".handoff-logs" / "credentials" / "service-token"
    execution_id = "11111111-1111-1111-1111-111111111111"
    record_path = repo / ".handoff-logs" / "runs" / f"{execution_id}.json"
    markdown = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    request = coding_payload(repo, "fixture", handoff_markdown=markdown)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps({"execution_id": execution_id, "task_id": "task-1", "agent_card_url": "http://127.0.0.1:9/card", "request": request})
        + "\n",
        encoding="utf-8",
    )
    (repo / ".handoff-logs" / "outstanding.json").write_text(
        json.dumps({"execution_id": execution_id, "run_id": "run-1", "run_record": str(record_path), "credential_file": str(token), "task_id": "task-1"})
        + "\n",
        encoding="utf-8",
    )
    status = main(["--repo", str(repo), "status"])
    status_err = capsys.readouterr()
    assert status == 2
    assert "connection not permitted (sandbox?)" in status_err.out and "server start" not in status_err.out
    resume = main(["--repo", str(repo), "resume"])
    resume_err = capsys.readouterr().err
    assert resume == 2
    assert "connection not permitted (sandbox?)" in resume_err and "server start" not in resume_err


def test_refused_connection_still_suggests_server_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _env = _approve(tmp_path)
    monkeypatch.setattr("handoff_a2a.integration.inspect_service", _running_state)
    from handoff_a2a.integration import main

    code = main(["--repo", str(repo), "execute"])
    err = capsys.readouterr().err
    assert code == 1
    assert "run handoff server start" in err
    assert "connection not permitted" not in err


@pytest.mark.server
def test_wait_timeout_names_the_run(tmp_path: Path) -> None:
    def once(root: Path, *, as_json: bool) -> subprocess.CompletedProcess[str]:
        root.mkdir()
        repo, env = managed_repo(root)
        write_handoff(repo, status="DRAFT", branch="main")
        (repo / ".fake-sleep").write_text("4\n", encoding="utf-8")
        config = json.loads((repo / ".handoff-config.json").read_text(encoding="utf-8"))
        config["a2a"]["wait_timeout_s"] = 1
        (repo / ".handoff-config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        assert handoff(env, "approve", str(repo)).returncode == 0
        assert handoff(env, "server", "start", str(repo)).returncode == 0
        try:
            args = ["execute", str(repo)] + (["--json"] if as_json else [])
            return handoff(env, *args)
        finally:
            stop_managed(repo)

    text = once(tmp_path / "text", as_json=False)
    assert text.returncode == 2, text.stdout + text.stderr
    assert "still in progress" in text.stderr and "handoff resume" in text.stderr
    assert "after 1" in text.stderr and "execution " in text.stderr
    as_json = once(tmp_path / "json", as_json=True)
    assert as_json.returncode == 2, as_json.stdout + as_json.stderr
    body = _json(as_json)
    assert body["wait_timeout"] is True and body["run_still_in_progress"] is True
    assert body["timeout_s"] == 1 and body["execution_id"]
    assert body["next"].startswith("handoff resume")


@pytest.mark.server
def test_executor_boundary_diff_is_in_the_manifest(tmp_path: Path) -> None:
    def run(root: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        root.mkdir()
        repo, env = managed_repo(root)
        write_handoff(repo, status="DRAFT", branch="main")
        (repo / ".fake-mode").write_text(mode + "\n", encoding="utf-8")
        assert handoff(env, "approve", str(repo)).returncode == 0
        assert handoff(env, "server", "start", str(repo)).returncode == 0
        try:
            result = handoff(env, "execute", str(repo))
        finally:
            stop_managed(repo)
        return result, _manifest(repo)

    rejected, bad = run(tmp_path / "edit", "edit_goal")
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "secret planner edit" in (bad.get("reason") or "")
    accepted, good = run(tmp_path / "strip", "strip_separator")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert good.get("boundary_note")


def test_append_on_a_crlf_body_does_not_add_an_extra_blank_line() -> None:
    from handoff_a2a.workspace import render_qa

    text = (
        "## Current Task\r\n\r\n**Status:** READY FOR QA\r\n\r\n"
        "## Execution Notes\r\n\r\nx\r\n\r\n---\r\n\r\n"
        "## QA Feedback\r\n\r\nAlready.\r\n\r\n"
    )
    updated = render_qa(text, "More.\n", status=None, append=True)
    assert updated.endswith("Already.\r\n\r\nMore.\r\n")
    assert "\r\n\r\n\r\n" not in updated.split("## QA Feedback", 1)[1]


def test_handoff_qa_writes_only_the_qa_section(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="READY FOR QA", branch="main")
    path = repo / "HANDOFF.md"
    text = path.read_text(encoding="utf-8").replace(
        "### Goal\n", "### Goal\n\nThe real section is not this `## QA Feedback` quote.\n"
    )
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    path.chmod(0o640)
    before = path.read_bytes()
    before_hash = approved_plan_hash(text)
    qa = tmp_path / "qa.md"
    qa.write_text("Approved after review.\n", encoding="utf-8")
    result = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    body = _json(result)
    assert body["kind"] == "qa" and body["schema"] == "urn:handoff-automation:cli-output:v1"
    assert body["status"] == "APPROVED"
    assert Path(body["backup"]).is_file()
    written = path.read_bytes()
    assert written.startswith(b"##") or b"\r\n" in written
    assert b"\r\n" in written
    assert stat_mode(path) == 0o640
    assert approved_plan_hash(path.read_text(encoding="utf-8")) == before_hash
    assert b"Approved after review." in written
    assert written.count(b"\n## QA Feedback") == 1
    assert before != written
    appended = handoff(env, "qa", str(repo), "--status", "CHANGES REQUESTED", "--file", str(qa), "--append")
    assert appended.returncode == 0, appended.stdout + appended.stderr
    assert "Status unchanged" in appended.stdout
    assert parse_handoff(path.read_text(encoding="utf-8")).status == "APPROVED"
    unchanged = path.read_bytes()
    (repo / ".handoff-logs" / "outstanding.json").write_text('{"run_id":"r"}\n', encoding="utf-8")
    refused = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert refused.returncode != 0 and path.read_bytes() == unchanged
    (repo / ".handoff-logs" / "outstanding.json").unlink()
    draft_text = text.replace("**Status:** READY FOR QA", "**Status:** DRAFT")
    path.write_text(draft_text, encoding="utf-8")
    draft = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert draft.returncode != 0 and "READY FOR QA" in draft.stderr
    assert path.read_text(encoding="utf-8") == draft_text
    execution = text.replace("**Status:** READY FOR QA", "**Status:** READY FOR EXECUTION")
    path.write_text(execution, encoding="utf-8")
    executing = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert executing.returncode != 0 and path.read_text(encoding="utf-8") == execution
    broken = text + "\n## QA Feedback\n\nextra\n"
    path.write_text(broken, encoding="utf-8")
    malformed = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert malformed.returncode != 0 and "QA Feedback" in malformed.stderr
    assert path.read_text(encoding="utf-8") == broken


def test_qa_append_from_ready_for_qa_sets_status_and_keeps_the_round(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    round1 = "Round 1 kept byte-for-byte.\n"
    write_handoff(repo, status="READY FOR QA", branch="main", qa=round1)
    path = repo / "HANDOFF.md"
    before = path.read_bytes()
    before_hash = approved_plan_hash(path.read_text(encoding="utf-8"))
    second = tmp_path / "r2.md"
    second.write_text("Round 2.\n", encoding="utf-8")
    result = handoff(env, "qa", str(repo), "--append", "--status", "APPROVED", "--file", str(second))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "appended QA Feedback" in result.stdout and "Status set to APPROVED" in result.stdout
    written = path.read_text(encoding="utf-8")
    assert "Round 1 kept byte-for-byte.\n\nRound 2.\n" in written
    assert parse_handoff(written).status == "APPROVED"
    assert approved_plan_hash(written) == before_hash
    backups = list((repo / ".handoff-logs").glob("HANDOFF.pre-qa-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before

    note = tmp_path / "note.md"
    note.write_text("Note after approval.\n", encoding="utf-8")
    appended = handoff(env, "qa", str(repo), "--append", "--status", "CHANGES REQUESTED", "--file", str(note))
    assert appended.returncode == 0, appended.stdout + appended.stderr
    assert "Status unchanged" in appended.stdout
    assert parse_handoff(path.read_text(encoding="utf-8")).status == "APPROVED"

    draft = path.read_text(encoding="utf-8").replace("**Status:** APPROVED", "**Status:** DRAFT")
    path.write_text(draft, encoding="utf-8")
    refused = handoff(env, "qa", str(repo), "--append", "--status", "APPROVED", "--file", str(second))
    assert refused.returncode != 0 and path.read_text(encoding="utf-8") == draft
    execution = draft.replace("**Status:** DRAFT", "**Status:** READY FOR EXECUTION")
    path.write_text(execution, encoding="utf-8")
    refused_exec = handoff(env, "qa", str(repo), "--append", "--status", "CHANGES REQUESTED", "--file", str(second))
    assert refused_exec.returncode != 0 and path.read_text(encoding="utf-8") == execution


def test_qa_on_mixed_endings_changes_only_the_body_and_status(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="READY FOR QA", branch="main")
    path = repo / "HANDOFF.md"
    text = path.read_text(encoding="utf-8")
    status_at = text.index("**Status:**")
    line_end = text.index("\n", status_at)
    mixed = text[: line_end + 1].encode() + text[line_end + 1 :].replace("\n", "\r\n").encode()
    path.write_bytes(mixed)
    path.chmod(0o644)
    qa = tmp_path / "qa.md"
    qa.write_text("Checked.\n", encoding="utf-8")
    result = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert result.returncode == 0, result.stdout + result.stderr
    written = path.read_bytes()
    assert stat_mode(path) == 0o644

    def outside(data: bytes) -> bytes:
        prefix, heading, _body = data.partition(b"## QA Feedback")
        prefix = prefix.replace(b"**Status:** READY FOR QA", b"**Status:**")
        prefix = prefix.replace(b"**Status:** APPROVED", b"**Status:**")
        return prefix + heading

    assert outside(written) == outside(mixed)
    assert b"Checked.\r\n" in written
    assert b"**Status:** APPROVED\n" in written


def test_qa_refuses_when_the_plan_already_changed(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="main")
    assert handoff(env, "approve", str(repo)).returncode == 0
    path = repo / "HANDOFF.md"
    text = path.read_text(encoding="utf-8")
    edited = text.replace(
        "Set app.py value according to the current round.", "Edited after approval."
    ).replace("**Status:** READY FOR EXECUTION", "**Status:** READY FOR QA")
    path.write_text(edited, encoding="utf-8")
    before = path.read_bytes()
    qa = tmp_path / "qa.md"
    qa.write_text("nope\n", encoding="utf-8")
    refused = handoff(env, "qa", str(repo), "--status", "APPROVED", "--file", str(qa))
    assert refused.returncode != 0, refused.stdout
    assert "delivered copy" in refused.stderr and "re-approve" in refused.stderr
    assert path.read_bytes() == before
    assert not list((repo / ".handoff-logs").glob("HANDOFF.pre-qa-*"))


def test_wait_flag_rejects_bad_values(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    for value in ("0", "-5", "abc", "1801"):
        for command in ("execute", "resume"):
            result = handoff(env, command, "--wait", value, str(repo))
            assert result.returncode == 1, (command, value, result.stderr)
            assert "positive number up to 1800" in result.stderr


@pytest.mark.server
def test_wait_flag_reports_the_value_used(tmp_path: Path) -> None:
    repo, env = managed_repo(tmp_path)
    write_handoff(repo, status="DRAFT", branch="main")
    (repo / ".fake-sleep").write_text("80\n", encoding="utf-8")
    config = json.loads((repo / ".handoff-config.json").read_text(encoding="utf-8"))
    config["a2a"]["wait_timeout_s"] = 5
    (repo / ".handoff-config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert handoff(env, "approve", str(repo)).returncode == 0
    assert handoff(env, "server", "start", str(repo)).returncode == 0
    try:
        started = time.monotonic()
        execute = handoff(env, "execute", str(repo), "--wait", "30", "--json", timeout=45)
        elapsed = time.monotonic() - started
        assert execute.returncode == 2, execute.stdout + execute.stderr
        body = _json(execute)
        assert body["timeout_s"] == 30 and body["wait_timeout"] is True
        assert 25 <= elapsed <= 40
        started = time.monotonic()
        resume = handoff(env, "resume", str(repo), "--wait", "30", "--json", timeout=45)
        elapsed = time.monotonic() - started
        assert resume.returncode == 2, resume.stdout + resume.stderr
        body = _json(resume)
        assert body["timeout_s"] == 30
        assert 25 <= elapsed <= 40
    finally:
        stop_managed(repo)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.server
def test_bad_headings_are_rejected_and_the_delivery_is_saved(tmp_path: Path) -> None:
    def run(root: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], dict, Path]:
        root.mkdir()
        repo, env = managed_repo(root)
        write_handoff(repo, status="DRAFT", branch="main")
        (repo / ".fake-mode").write_text(mode + "\n", encoding="utf-8")
        assert handoff(env, "approve", str(repo)).returncode == 0
        assert handoff(env, "server", "start", str(repo)).returncode == 0
        try:
            result = handoff(env, "execute", str(repo))
        finally:
            stop_managed(repo)
        return result, _manifest(repo), repo

    for mode, needle in (
        ("duplicate_heading", "QA Feedback"),
        ("second_current_task", "Current Task"),
        ("drop_execution_notes", "Execution Notes"),
        ("reorder_sections", "before"),
    ):
        result, manifest, repo = run(tmp_path / mode, mode)
        assert result.returncode != 0, result.stdout + result.stderr
        assert needle in (manifest.get("reason") or "")
        delivered = Path(str(manifest["delivered_handoff"]))
        assert delivered.is_file()
        assert manifest["delivered_handoff_sha256"]
        evidence = Path(str((manifest.get("evidence") or {}).get("evidence_dir") or ""))
        if evidence.is_dir():
            saved = evidence / "handoff-delivered.md"
            if saved.is_file():
                assert saved.read_bytes() == delivered.read_bytes()

    ok, manifest, repo = run(tmp_path / "ok", "success")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    delivered = Path(str(manifest["delivered_handoff"]))
    data = delivered.read_bytes()
    assert manifest["delivered_handoff_sha256"] == __import__(
        "handoff_a2a.contracts", fromlist=["snapshot_sha256"]
    ).snapshot_sha256(data)
    from handoff_a2a.integration import _reconcile

    recorded = _reconcile(
        repo,
        outstanding={"run_id": "missing-copy", "execution_id": "missing-copy"},
        task={"status": {"state": "TASK_STATE_FAILED"}},
        result={"evidence_dir": str(tmp_path / "no-evidence"), "worker_launched": False},
        unresolved=False,
        reason="older server",
    )
    assert recorded["delivered_handoff"] is None
