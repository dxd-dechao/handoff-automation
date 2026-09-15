from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_harness import make_repo, write_handoff
from handoff_a2a.config import ConfigError, load_config
from handoff_a2a.contracts import CODING_TASK_PROFILE, parse_coding_request, snapshot_sha256
from handoff_a2a.workflow import approve, archive_current, consume_round, reserve_round, rounds_available
from handoff_a2a.workspace import GitWorkspace, approved_plan_hash, planner_fingerprint


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
