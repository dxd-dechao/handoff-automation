from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from a2a.types.a2a_pb2 import CancelTaskRequest
from a2a.utils.errors import TaskNotCancelableError

from handoff_a2a.__main__ import main
from handoff_a2a.client import CodingClient, coding_result_from_task
from handoff_a2a.contracts import parse_coding_request, snapshot_sha256
from a2a_harness import (
    RunningServer,
    coding_payload,
    git,
    make_fake_claude,
    make_repo,
    make_server_config,
    write_handoff,
)


def _state(task: dict) -> str:
    return (task.get("status") or {}).get("state")


async def _client(card_url: str, token: str) -> CodingClient:
    client = CodingClient(card_url, token)
    await client.connect()
    return client


async def _submit(client: CodingClient, payload: dict) -> dict:
    request = parse_coding_request(payload)
    assert "model" not in request.to_dict()
    return await client.submit(request)


@pytest.mark.asyncio
async def test_roundtrip_success_and_correction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-not-leak")
    with RunningServer(config) as server:
        client = await _client(server.card_url, token)
        try:
            card = client._card
            assert card is not None
            assert any(s.id == "implement-approved-coding-task" for s in card.skills)
            assert any(i.protocol_binding == "JSONRPC" for i in card.supported_interfaces)

            first = coding_payload(repo, config.workspace_id, execution_id=str(uuid4()), run_id="run-a")
            submitted = await _submit(client, first)
            terminal = await client.wait(submitted["id"])
            assert _state(terminal) == "TASK_STATE_COMPLETED"
            result = coding_result_from_task(terminal)
            assert result is not None
            assert result["run_id"] == "run-a"
            assert result["execution_id"] == first["execution_id"]
            assert result["task_id"] == submitted["id"]
            assert result["cost_usd"] == 0.0
            assert result["usage"]["input_tokens"] == 1
            assert result["usage"]["cache_creation_input_tokens"] == 0
            assert (repo / "app.py").read_text() == "value = 1\n"
            assert "READY FOR QA" in (repo / "HANDOFF.md").read_text()
            assert (evidence / first["execution_id"] / "result.json").is_file()
            assert (evidence / first["execution_id"] / "handoff.md").is_file()
            assert not (repo / ".handoff-logs" / "execute.lock").exists()
            assert (repo / "AUTH_PROBE").read_text() == ""

            write_handoff(
                repo,
                status="CHANGES REQUESTED",
                branch="main",
                notes="implemented value=1",
                qa="app.py must set value = 2, not 1.",
            )
            (repo / ".fake-mode").write_text("fix\n", encoding="utf-8")
            second = coding_payload(
                repo,
                config.workspace_id,
                workflow_id=first["workflow_id"],
                run_id="run-b",
                execution_id=str(uuid4()),
                iteration=2,
            )
            submitted2 = await _submit(client, second)
            assert submitted2["id"] != submitted["id"]
            terminal2 = await client.wait(submitted2["id"])
            assert _state(terminal2) == "TASK_STATE_COMPLETED"
            result2 = coding_result_from_task(terminal2)
            assert result2 is not None
            assert result2["iteration"] == 2
            assert result2["task_id"] == submitted2["id"]
            assert (repo / "app.py").read_text() == "value = 2\n"
            assert "value must be 2" in (repo / "HANDOFF.md").read_text()
            assert (evidence / second["execution_id"] / "result.json").is_file()
            assert (evidence / first["execution_id"] / "result.json").is_file()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_refusals_before_worker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    with RunningServer(config) as server:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as anon:
            card = (await anon.get(server.card_url)).json()
            assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"

        client = await _client(server.card_url, token)
        try:
            lock = repo / ".handoff-logs" / "execute.lock"
            lock.mkdir(parents=True)
            busy = await _submit(client, coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
            busy_term = await client.wait(busy["id"])
            assert _state(busy_term) == "TASK_STATE_REJECTED"
            assert not (repo / "WORKER_STARTED").exists()
            lock.rmdir()

            bad_ws = await _submit(
                client,
                coding_payload(repo, "other", execution_id=str(uuid4())),
            )
            assert _state(await client.wait(bad_ws["id"])) == "TASK_STATE_REJECTED"
            assert not (repo / "WORKER_STARTED").exists()

            mismatched = coding_payload(
                repo,
                config.workspace_id,
                execution_id=str(uuid4()),
                handoff_markdown="# different\n",
            )
            mismatched["request_sha256"] = snapshot_sha256("# different\n")
            snap = await _submit(client, mismatched)
            assert _state(await client.wait(snap["id"])) == "TASK_STATE_REJECTED"
            assert not (repo / "WORKER_STARTED").exists()

            stale = await _submit(
                client,
                coding_payload(
                    repo,
                    config.workspace_id,
                    expected_head="0" * 40,
                    execution_id=str(uuid4()),
                ),
            )
            assert _state(await client.wait(stale["id"])) == "TASK_STATE_REJECTED"
            assert not (repo / "WORKER_STARTED").exists()
        finally:
            await client.close()

        wrong = CodingClient(server.card_url, "wrong-token")
        await wrong.connect()
        with pytest.raises(Exception):
            await _submit(wrong, coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
        await wrong.close()
        assert not (repo / "WORKER_STARTED").exists()


async def _run_mode(tmp_path: Path, mode: str) -> tuple[dict, dict, Path]:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    (repo / ".fake-mode").write_text(mode + "\n", encoding="utf-8")
    with RunningServer(config) as server:
        client = await _client(server.card_url, token)
        try:
            submitted = await _submit(
                client, coding_payload(repo, config.workspace_id, execution_id=str(uuid4()))
            )
            terminal = await client.wait(submitted["id"])
            result = coding_result_from_task(terminal)
            assert result is not None
            return terminal, result, evidence
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_nonzero_exit_is_failed(tmp_path: Path) -> None:
    terminal, result, _evidence = await _run_mode(tmp_path, "nonzero")
    assert _state(terminal) == "TASK_STATE_FAILED"
    assert result["exit_code"] == 3
    assert result["worker_launched"] is True


@pytest.mark.asyncio
async def test_exit_zero_provider_error_is_failed(tmp_path: Path) -> None:
    terminal, result, _evidence = await _run_mode(tmp_path, "provider_error")
    assert _state(terminal) == "TASK_STATE_FAILED"
    assert result["exit_code"] == 0
    assert result["provider_error"] is True


@pytest.mark.asyncio
async def test_malformed_result_is_failed(tmp_path: Path) -> None:
    terminal, result, _evidence = await _run_mode(tmp_path, "malformed")
    assert _state(terminal) == "TASK_STATE_FAILED"
    assert result["invalid_output"] is True


@pytest.mark.asyncio
async def test_wrong_shape_json_object_is_failed(tmp_path: Path) -> None:
    terminal, result, evidence = await _run_mode(tmp_path, "wrong_shape")
    assert _state(terminal) == "TASK_STATE_FAILED"
    assert result["invalid_output"] is True
    assert result["provider_error"] is False
    raw = (evidence / result["execution_id"] / "stdout.json").read_text(encoding="utf-8")
    assert "not a Claude result" in raw


@pytest.mark.asyncio
async def test_missing_cost_stays_null_and_zero_usage_survives(tmp_path: Path) -> None:
    terminal, result, evidence = await _run_mode(tmp_path, "cost_missing")
    assert _state(terminal) == "TASK_STATE_COMPLETED"
    assert result.get("cost_usd") is None
    assert result["usage"]["input_tokens"] == 4
    assert (evidence / result["execution_id"] / "stderr.txt").exists()


@pytest.mark.asyncio
async def test_zero_cost_survives_roundtrip(tmp_path: Path) -> None:
    terminal, result, _evidence = await _run_mode(tmp_path, "success")
    assert _state(terminal) == "TASK_STATE_COMPLETED"
    assert result["cost_usd"] == 0.0
    assert result["usage"]["cache_creation_input_tokens"] == 0



@pytest.mark.asyncio
async def test_cancel_is_not_supported_and_does_not_stop_worker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    (repo / ".fake-sleep").write_text("0.8\n", encoding="utf-8")
    with RunningServer(config) as server:
        client = await _client(server.card_url, token)
        try:
            submitted = await _submit(
                client, coding_payload(repo, config.workspace_id, execution_id=str(uuid4()))
            )
            with pytest.raises(TaskNotCancelableError):
                await client._transport.cancel_task(CancelTaskRequest(id=submitted["id"]))
            terminal = await client.wait(submitted["id"], timeout=15)
            assert _state(terminal) == "TASK_STATE_COMPLETED"
            assert (repo / "app.py").read_text() == "value = 1\n"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_lock_held_until_evidence_then_released(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    with RunningServer(config) as server:
        client = await _client(server.card_url, token)
        try:
            execution_id = str(uuid4())
            submitted = await _submit(
                client, coding_payload(repo, config.workspace_id, execution_id=execution_id)
            )
            terminal = await client.wait(submitted["id"])
            assert _state(terminal) == "TASK_STATE_COMPLETED"
            assert (evidence / execution_id / "result.json").is_file()
            assert not (repo / ".handoff-logs" / "execute.lock").exists()
            assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        finally:
            await client.close()


def _run_cli(repo: Path, card_url: str, token_path: Path, workspace_id: str) -> tuple[int, dict]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(
            [
                "execute",
                "--repo",
                str(repo),
                "--agent-card-url",
                card_url,
                "--credential-file",
                str(token_path),
                "--workspace-id",
                workspace_id,
            ]
        )
    printed = json.loads(buffer.getvalue())
    return code, printed


def test_cli_success_exits_zero(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    with RunningServer(config) as server:
        code, printed = _run_cli(repo, server.card_url, token_path, config.workspace_id)
    assert code == 0
    assert printed["state"] == "TASK_STATE_COMPLETED"
    assert printed["task_id"]
    assert printed["execution_id"]
    assert printed["result"]["cost_usd"] == 0.0


def test_cli_worker_failure_exits_nonzero(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("nonzero\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    with RunningServer(config) as server:
        code, printed = _run_cli(repo, server.card_url, token_path, config.workspace_id)
    assert code != 0
    assert code != 2
    assert printed["state"] == "TASK_STATE_FAILED"
    assert printed["task_id"]
    assert printed["execution_id"]
    assert printed["reason"]
    assert printed["result"]["exit_code"] == 3


def test_cli_preworker_rejection_exits_nonzero_with_reason(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    lock = repo / ".handoff-logs" / "execute.lock"
    lock.mkdir(parents=True)
    with RunningServer(config) as server:
        code, printed = _run_cli(repo, server.card_url, token_path, config.workspace_id)
    assert code != 0
    assert code != 2
    assert printed["state"] == "TASK_STATE_REJECTED"
    assert printed["task_id"]
    assert printed["execution_id"]
    assert printed["reason"]
    assert printed.get("result") in ({}, None) or printed["reason"]
    assert not (repo / "WORKER_STARTED").exists()
