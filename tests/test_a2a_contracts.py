from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_a2a.adapters.claude import RITUAL_PROMPT, child_environment, parse_claude_json
from handoff_a2a.client import execute_exit_code, task_reason
from handoff_a2a.contracts import (
    CODING_TASK_PROFILE,
    ContractError,
    parse_coding_request,
    snapshot_sha256,
)
from handoff_a2a.server import load_server_config
from handoff_a2a.workspace import parse_handoff, planner_fingerprint, read_status


def test_snapshot_hash_is_utf8_bytes() -> None:
    text = "café\n"
    assert snapshot_sha256(text) == snapshot_sha256(text.encode("utf-8"))


def test_request_requires_matching_hash() -> None:
    markdown = "# hi\n"
    with pytest.raises(ContractError, match="request_sha256"):
        parse_coding_request(
            {
                "schema": CODING_TASK_PROFILE,
                "workflow_id": "wf",
                "run_id": "run",
                "execution_id": "11111111-1111-1111-1111-111111111111",
                "iteration": 1,
                "workspace_id": "fixture",
                "expected_branch": "main",
                "expected_head": "abc",
                "handoff_markdown": markdown,
                "request_sha256": "00" * 32,
            }
        )


def test_iteration_must_be_positive() -> None:
    markdown = "x"
    base = {
        "schema": CODING_TASK_PROFILE,
        "workflow_id": "wf",
        "run_id": "run",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "fixture",
        "expected_branch": "main",
        "expected_head": "abc",
        "handoff_markdown": markdown,
        "request_sha256": snapshot_sha256(markdown),
    }
    with pytest.raises(ContractError, match="iteration"):
        parse_coding_request({**base, "iteration": 0})
    parsed = parse_coding_request({**base, "iteration": 1.0})
    assert parsed.iteration == 1
    assert "model" not in parsed.to_dict()
    assert "provider" not in parsed.to_dict()


def test_handoff_status_and_planner_fingerprint_ignore_executor_fields() -> None:
    first = """## Current Task

**Status:** READY FOR EXECUTION

**Branch:** main

### Goal

Do the thing.

## Execution Notes

old notes

## QA Feedback

Not run.
"""
    second = """## Current Task

**Status:** READY FOR QA

**Branch:** main

### Goal

Do the thing.

## Execution Notes

new notes from executor

## QA Feedback

Not run.
"""
    assert read_status(first) == "READY FOR EXECUTION"
    assert planner_fingerprint(first) == planner_fingerprint(second)
    changed_plan = second.replace("Do the thing.", "Do something else.")
    assert planner_fingerprint(first) != planner_fingerprint(changed_plan)
    doc = parse_handoff(second)
    assert "new notes from executor" in doc.execution_notes


def test_auth_stripping_and_ritual_prompt() -> None:
    env = child_environment(
        {
            "PATH": "/bin",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "CLAUDECODE": "1",
            "HANDOFF_FAKE_MODE": "ok",
        }
    )
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDECODE" not in env
    assert env["HANDOFF_FAKE_MODE"] == "ok"
    assert RITUAL_PROMPT == "execute the handoff"


def test_zero_cost_survives_parse_and_missing_cost_is_invalid_json_object_ok() -> None:
    parsed, invalid = parse_claude_json(
        '{"type": "result", "is_error": false, "total_cost_usd": 0.0, "result": "ok", "usage": {"input_tokens": 0}}'
    )
    assert invalid is False
    assert parsed is not None
    assert parsed["total_cost_usd"] == 0.0
    assert parsed["usage"]["input_tokens"] == 0
    missing_cost, missing_invalid = parse_claude_json(
        '{"type": "result", "is_error": false, "result": "ok"}'
    )
    assert missing_invalid is False
    assert missing_cost is not None
    _, bad = parse_claude_json("not json")
    assert bad is True


def test_cli_exit_codes_distinguish_success_from_unsuccessful_terminal() -> None:
    assert execute_exit_code("TASK_STATE_COMPLETED") == 0
    assert execute_exit_code("TASK_STATE_FAILED") != 0
    assert execute_exit_code("TASK_STATE_REJECTED") != 0
    assert task_reason(
        {"status": {"state": "TASK_STATE_REJECTED", "message": {"parts": [{"data": {"reason": "busy"}}]}}}
    ) == "busy"


def test_wrong_shape_json_object_is_invalid_claude_result() -> None:
    parsed, invalid = parse_claude_json('{"unrelated": "not a Claude result"}')
    assert invalid is True
    assert parsed is None
    missing_outcome, missing_invalid = parse_claude_json('{"type": "result"}')
    assert missing_invalid is True
    assert missing_outcome is None


def test_skill_has_valid_metadata_and_existing_cli_intents() -> None:
    text = Path("skills/handoff-cli/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    closing = text.find("\n---\n", 4)
    assert closing != -1
    front = text[4:closing]
    assert "name: handoff-cli" in front
    description = front.split("description:", 1)[1]
    assert description.strip()
    body = text[closing:]
    for command in ("handoff execute", "handoff status", "handoff runs"):
        assert command in body



def test_non_loopback_config_is_rejected(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("abc\n", encoding="utf-8")
    cfg = tmp_path / "server.json"
    cfg.write_text(
        json_dumps(
            {
                "host": "0.0.0.0",
                "port": 9,
                "workspace_id": "x",
                "workspace_path": str(tmp_path),
                "credential_file": str(token),
                "evidence_dir": str(tmp_path / "ev"),
                "claude": {"binary": "/bin/false", "model": "x"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="loopback"):
        load_server_config(cfg)


def json_dumps(payload: dict) -> str:
    return json.dumps(payload)
