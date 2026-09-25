"""Preflight, sandbox probes, skill copies, and delivery boundaries."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from a2a_harness import git, handoff, make_repo, managed_repo, write_handoff
from handoff_a2a.processes import LIVENESS_DEAD, LIVENESS_UNKNOWN, process_liveness
from handoff_a2a.providers import failure_kind
from handoff_a2a.workspace import GitWorkspace, approved_plan_hash, parse_handoff


def _json(result) -> dict:
    return json.loads(result.stdout)


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
    assert (copy / "SKILL.md").read_bytes() == before
    init = handoff(env, "init", str(repo), "--planner", "claude")
    assert init.returncode == 0, init.stderr
    assert str(copy) in init.stdout
    assert not (repo / ".claude" / "skills" / "handoff-cli").exists()
