from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_harness import git, make_repo, write_handoff
from handoff_a2a.config import ConfigError, load_config
from handoff_a2a.contracts import CODING_TASK_PROFILE, parse_coding_request, snapshot_sha256
from handoff_a2a.workflow import (
    WorkflowError,
    approve,
    archive_current,
    consume_round,
    load_workflow,
    reserve_round,
    rounds_available,
    save_workflow,
)
from handoff_a2a.workspace import (
    GitWorkspace,
    approved_plan_hash,
    is_workflow_path,
    planner_fingerprint,
)


def test_approved_plan_hash_ignores_status_notes_and_qa(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = write_handoff(repo, status="DRAFT", notes="none", qa="none")
    second = write_handoff(repo, status="READY FOR EXECUTION", notes="executor", qa="none")
    third = write_handoff(repo, status="CHANGES REQUESTED", notes="executor", qa="please fix")
    changed = third.replace("Set app.py value according to the current round.", "Do something else.")
    assert approved_plan_hash(first) == approved_plan_hash(second)
    assert approved_plan_hash(first) == approved_plan_hash(third)
    assert approved_plan_hash(first) != approved_plan_hash(changed)
    assert planner_fingerprint(second) != planner_fingerprint(third)


def test_fingerprint_excludes_handoff_and_detects_code(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    gitws = GitWorkspace("fixture", repo)
    clean = gitws.code_fingerprint()
    assert gitws.code_is_clean()
    write_handoff(repo, status="DRAFT", notes="changed notes")
    assert gitws.code_fingerprint() == clean
    (repo / "app.py").write_text("value = 9\n", encoding="utf-8")
    dirty = gitws.code_fingerprint()
    assert dirty != clean
    assert not gitws.code_is_clean()
    (repo / "app.py").write_text("value = 0\n", encoding="utf-8")
    (repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert gitws.code_fingerprint() != clean
    assert not gitws.code_is_clean()


def _original_code_fingerprint(gitws: GitWorkspace) -> str:
    """The pre-A9 payload: untracked files except workflow paths, including bytecode."""
    untracked: list[list[str]] = []
    for rel in gitws._untracked_files():
        if is_workflow_path(rel):
            continue
        path = gitws.path / rel
        data = path.read_bytes() if path.is_file() else b""
        untracked.append([rel, snapshot_sha256(data)])
    untracked.sort(key=lambda item: item[0])
    payload = {
        "branch": gitws.current_branch(),
        "head": gitws.current_head(),
        "index": snapshot_sha256(gitws.git_bytes("diff", "--cached", "--binary")),
        "worktree": snapshot_sha256(gitws.git_bytes("diff", "--binary")),
        "untracked": untracked,
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return snapshot_sha256(encoded)


def _no_global_pyc_ignore(repo: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty-exclude"
    empty.write_text("", encoding="utf-8")
    git(repo, "config", "core.excludesFile", str(empty))


def test_untracked_bytecode_is_not_code(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _no_global_pyc_ignore(repo, tmp_path)
    gitws = GitWorkspace("fixture", repo)
    clean = gitws.code_fingerprint()
    assert clean == _original_code_fingerprint(gitws)
    cache = repo / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    pyc = cache / "m.cpython-312.pyc"
    stray = repo / "x.pyc"
    pyc.write_bytes(b"one")
    stray.write_bytes(b"two")
    assert "pkg/__pycache__/m.cpython-312.pyc" in gitws.git("status", "--porcelain", "-uall")
    assert gitws.code_fingerprint() == clean
    assert gitws.dirty_code_paths() == []
    pyc.write_bytes(b"changed")
    stray.write_bytes(b"changed")
    assert gitws.code_fingerprint() == clean
    assert gitws.dirty_code_paths() == []
    pyc.unlink()
    stray.unlink()
    cache.rmdir()
    (repo / "pkg").rmdir()
    assert gitws.code_fingerprint() == clean
    assert gitws.dirty_code_paths() == []


def test_tracked_bytecode_still_counts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _no_global_pyc_ignore(repo, tmp_path)
    tracked = repo / "vendor.pyc"
    tracked.write_bytes(b"committed")
    git(repo, "add", "-f", "vendor.pyc")
    git(repo, "commit", "-qm", "commit bytecode")
    gitws = GitWorkspace("fixture", repo)
    clean = gitws.code_fingerprint()
    tracked.write_bytes(b"modified")
    assert gitws.code_fingerprint() != clean
    assert "vendor.pyc" in gitws.dirty_code_paths()


def test_rebaseline_on_changed_plan_only(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    first = approve(repo, workspace_id="fixture", max_rounds=3, current_fingerprint="should-not-apply")
    assert first["post_run_fingerprint"] is None
    assert first["workflow_id"]
    workflow_id = first["workflow_id"]
    recorded = "a" * 64
    first["post_run_fingerprint"] = recorded
    first["post_run_branch"] = "main"
    first["rounds_used"] = 1
    first["consumed_execution_ids"] = ["exec-1"]
    save_workflow(repo, first)
    same = approve(
        repo,
        workspace_id="fixture",
        max_rounds=3,
        current_fingerprint="b" * 64,
        current_branch="main",
        dirty_code_paths=[],
    )
    assert same["post_run_fingerprint"] == recorded
    assert same["workflow_id"] == workflow_id
    assert "rebaselined_at" not in same
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    (repo / "HANDOFF.md").write_text(
        text.replace("Set app.py value according to the current round.", "Narrower goal."),
        encoding="utf-8",
    )
    (repo / "app.py").write_text("value = 9\n", encoding="utf-8")
    before = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    before_workflow = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    with pytest.raises(WorkflowError, match="app.py"):
        approve(
            repo,
            workspace_id="fixture",
            max_rounds=3,
            current_fingerprint="c" * 64,
            current_branch="main",
            dirty_code_paths=["app.py"],
        )
    assert (repo / "HANDOFF.md").read_text(encoding="utf-8") == before
    assert json.loads((repo / ".handoff-logs" / "workflow.json").read_text())["approved_plan_hash"] == before_workflow["approved_plan_hash"]
    (repo / "app.py").write_text("value = 0\n", encoding="utf-8")
    updated = approve(
        repo,
        workspace_id="fixture",
        max_rounds=3,
        current_fingerprint="c" * 64,
        current_branch="main",
        dirty_code_paths=[],
    )
    assert updated["workflow_id"] == workflow_id
    assert updated["rounds_used"] == 1
    assert updated["post_run_fingerprint"] == "c" * 64
    assert updated["post_run_branch"] == "main"
    assert updated["previous_post_run_fingerprint"] == recorded
    assert updated["rebaselined_at"]
    assert load_workflow(repo)["post_run_fingerprint"] == "c" * 64


def test_rebaseline_skipped_when_fingerprint_is_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    first = approve(repo, workspace_id="fixture", max_rounds=3)
    recorded = "d" * 64
    first["post_run_fingerprint"] = recorded
    first["post_run_branch"] = "main"
    first["rounds_used"] = 1
    save_workflow(repo, first)
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    (repo / "HANDOFF.md").write_text(
        text.replace("Set app.py value according to the current round.", "Same tree, new plan."),
        encoding="utf-8",
    )
    again = approve(
        repo,
        workspace_id="fixture",
        max_rounds=3,
        current_fingerprint=recorded,
        current_branch="main",
        dirty_code_paths=[],
    )
    assert "rebaselined_at" not in again
    assert "previous_post_run_fingerprint" not in again
    assert again["post_run_fingerprint"] == recorded


def test_optional_fingerprint_is_additive() -> None:
    markdown = "# hi\n"
    base = {
        "schema": CODING_TASK_PROFILE,
        "workflow_id": "wf",
        "run_id": "run",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "iteration": 1,
        "workspace_id": "fixture",
        "expected_branch": "main",
        "expected_head": "abc",
        "handoff_markdown": markdown,
        "request_sha256": snapshot_sha256(markdown),
    }
    plain = parse_coding_request(base)
    assert "expected_code_fingerprint" not in plain.to_dict()
    with_fp = parse_coding_request({**base, "expected_code_fingerprint": "abc123"})
    assert with_fp.expected_code_fingerprint == "abc123"


def test_approval_round_and_supersede(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    workflow = approve(repo, workspace_id="fixture", max_rounds=3)
    first_id = workflow["workflow_id"]
    again = approve(repo, workspace_id="fixture", max_rounds=3)
    assert again["workflow_id"] == first_id
    assert again["rounds_used"] == 0
    write_handoff(repo, status="DRAFT", notes="executor", qa="new scope that changes the goal")
    # Scope change must alter Current Task body, not only QA/notes.
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    (repo / "HANDOFF.md").write_text(
        text.replace("Set app.py value according to the current round.", "Narrowed remaining work."),
        encoding="utf-8",
    )
    updated = approve(repo, workspace_id="fixture", max_rounds=3)
    assert updated["workflow_id"] == first_id
    assert updated["approved_plan_hash"] != workflow["approved_plan_hash"]
    assert updated["rounds_used"] == 0
    reserve_round(repo, updated, "exec-1")
    consume_round(repo, updated, "exec-1")
    updated = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    consume_round(repo, updated, "exec-2")
    updated = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    consume_round(repo, updated, "exec-3")
    updated = json.loads((repo / ".handoff-logs" / "workflow.json").read_text())
    assert updated["rounds_used"] == 3
    assert rounds_available(updated) == 0
    archive_current(repo, superseded=True, goal="smaller follow-up")
    write_handoff(repo, status="DRAFT", qa="narrow task")
    successor = approve(repo, workspace_id="fixture", max_rounds=3)
    assert successor["parent_workflow_id"] == first_id
    assert successor["workflow_id"] != first_id
    assert successor["rounds_used"] == 0


def test_missing_config_is_legacy_and_unknown_transport_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert load_config(repo) is None
    (repo / ".handoff-config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed"):
        load_config(repo)
    (repo / ".handoff-config.json").write_text(
        json.dumps({"transport": "swarm"}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unknown transport"):
        load_config(repo)


def test_structure_rule_and_pinned_hash() -> None:
    from handoff_a2a.workspace import HandoffStructureError, handoff_structure, planner_fingerprint

    pin = "## Current Task\n\n**Status:** READY\n\nplan body\n"
    assert approved_plan_hash(pin) == "99f8cd04e18a04b4c2268d9804b1fc387cf3dc41923493896f7ca35c7b76ce9d"
    good = (
        "## Current Task\n\n**Status:** READY FOR QA\n\nquote `## QA Feedback` inline\n\n"
        "## Execution Notes\n\nnotes\n\n## QA Feedback\n\nok\n"
    )
    assert handoff_structure(good) == []
    assert handoff_structure(good.replace("\n", "\r\n")) == []
    duplicated = good + "\n## QA Feedback\n\nagain\n"
    assert any("## QA Feedback" in item and "2 times" in item for item in handoff_structure(duplicated))
    with pytest.raises(HandoffStructureError, match="QA Feedback"):
        planner_fingerprint(duplicated)
    missing = good.replace("## Execution Notes\n\nnotes\n\n", "")
    assert any("Execution Notes" in item and "missing" in item for item in handoff_structure(missing))
    swapped = (
        "## Current Task\n\n**Status:** READY FOR QA\n\nplan\n\n"
        "## QA Feedback\n\nok\n\n## Execution Notes\n\nnotes\n"
    )
    assert any("QA Feedback" in item and "before" in item for item in handoff_structure(swapped))


def test_archive_refuses_a_damaged_or_changed_plan(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    approve(repo, workspace_id="fixture", max_rounds=3)
    original = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    approved = original.replace("**Status:** READY FOR EXECUTION", "**Status:** APPROVED")
    (repo / "HANDOFF.md").write_text(approved, encoding="utf-8")
    workflow_path = repo / ".handoff-logs" / "workflow.json"
    before = workflow_path.read_bytes()
    archive_current(repo, superseded=False, goal="kept")
    assert (repo / "HANDOFF-ARCHIVE.md").is_file()
    workflow_path.write_bytes(before)
    (repo / "HANDOFF-ARCHIVE.md").unlink()
    changed = approved.replace("Set app.py value according to the current round.", "truncated plan")
    (repo / "HANDOFF.md").write_text(changed, encoding="utf-8")
    with pytest.raises(WorkflowError, match="delivered.md"):
        archive_current(repo, superseded=False, goal="nope")
    with pytest.raises(WorkflowError, match="pre-qa"):
        archive_current(repo, superseded=True, goal="nope")
    assert not (repo / "HANDOFF-ARCHIVE.md").exists()
    assert workflow_path.read_bytes() == before
    broken = approved + "\n## QA Feedback\n\nextra\n"
    (repo / "HANDOFF.md").write_text(broken, encoding="utf-8")
    with pytest.raises(WorkflowError, match="QA Feedback"):
        archive_current(repo, superseded=False, goal="nope")
    assert workflow_path.read_bytes() == before


def test_archive_refuses_to_append_the_same_current_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    approve(repo, workspace_id="fixture", max_rounds=3)
    original = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    approved = original.replace("**Status:** READY FOR EXECUTION", "**Status:** APPROVED")
    (repo / "HANDOFF.md").write_text(approved, encoding="utf-8")
    workflow_path = repo / ".handoff-logs" / "workflow.json"
    workflow_before = workflow_path.read_bytes()
    archive_current(repo, superseded=False, goal="kept")
    archived = (repo / "HANDOFF-ARCHIVE.md").read_bytes()
    workflow_path.write_bytes(workflow_before)
    with pytest.raises(
        WorkflowError,
        match=r"already archived as entry 1 \([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}\); plan the next task first",
    ):
        archive_current(repo, superseded=False, goal="again")
    assert (repo / "HANDOFF-ARCHIVE.md").read_bytes() == archived
    assert workflow_path.read_bytes() == workflow_before
    workflow_path.unlink()
    (repo / "HANDOFF.md").write_text(approved.replace("**Status:** APPROVED", "**Status:** READY FOR QA"), encoding="utf-8")
    with pytest.raises(WorkflowError, match="already archived as entry 1"):
        archive_current(repo, superseded=False, goal="status only")
    assert (repo / "HANDOFF-ARCHIVE.md").read_bytes() == archived
    (repo / "HANDOFF.md").write_bytes(approved.replace("\n", "\r\n").encode())
    with pytest.raises(WorkflowError, match="already archived as entry 1"):
        archive_current(repo, superseded=False, goal="crlf")
    assert (repo / "HANDOFF-ARCHIVE.md").read_bytes() == archived
    changed = approved.replace("Set app.py value according to the current round.", "A different plan.")
    (repo / "HANDOFF.md").write_text(changed, encoding="utf-8")
    archive_current(repo, superseded=False, goal="changed")
    assert (repo / "HANDOFF-ARCHIVE.md").read_text(encoding="utf-8").count("\n# Archived ") == 2
    (repo / "HANDOFF.md").write_text(approved, encoding="utf-8")
    archive_current(repo, superseded=False, goal="older plan")
    assert (repo / "HANDOFF-ARCHIVE.md").read_text(encoding="utf-8").count("\n# Archived ") == 3


def test_superseded_archive_refuses_the_same_current_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_handoff(repo, status="DRAFT")
    workflow = approve(repo, workspace_id="fixture", max_rounds=3)
    for name in ("exec-1", "exec-2", "exec-3"):
        reserve_round(repo, workflow, name)
        consume_round(repo, workflow, name)
        workflow = json.loads((repo / ".handoff-logs" / "workflow.json").read_text(encoding="utf-8"))
    archive_current(repo, superseded=True, goal="smaller follow-up")
    archived = (repo / "HANDOFF-ARCHIVE.md").read_bytes()
    with pytest.raises(WorkflowError, match=r"already archived as entry 1 \(.*\); plan the next task first"):
        archive_current(repo, superseded=True, goal="smaller follow-up")
    assert (repo / "HANDOFF-ARCHIVE.md").read_bytes() == archived
    text = (repo / "HANDOFF.md").read_text(encoding="utf-8")
    (repo / "HANDOFF.md").write_text(
        text.replace("Set app.py value according to the current round.", "Narrower work."),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="no workflow to supersede"):
        archive_current(repo, superseded=True, goal="other")
    assert (repo / "HANDOFF-ARCHIVE.md").read_bytes() == archived


def test_decorated_branch_field_names_the_plain_branch() -> None:
    from handoff_a2a.workspace import read_declared_branch

    assert read_declared_branch("**Branch:** `live/clamp`\n") == "live/clamp"
    assert read_declared_branch("**Branch:** live/clamp\n") == "live/clamp"
    assert read_declared_branch("**Branch:** `a` or `b`\n") == "`a` or `b`"  # not a single code span
