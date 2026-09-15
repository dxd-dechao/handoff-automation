from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from handoff_a2a.client import (
    UnresolvedExecution,
    cancel_from_record,
    card_supports_durable_dedup,
    initial_run_record,
    persist_run_record,
    resume_from_record,
    run_record_path,
    status_from_record,
)
from handoff_a2a.contracts import CODING_TASK_PROFILE, parse_coding_request
from handoff_a2a.processes import process_start_identity
from handoff_a2a.store import SqliteState
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


def _launches(repo: Path) -> int:
    path = repo / "WORKER_LAUNCHES"
    if not path.is_file():
        return 0
    return int(path.read_text(encoding="utf-8").strip() or "0")


def _wait_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.asyncio
async def test_duplicate_and_replay_share_one_worker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    execution_id = str(uuid4())
    payload = coding_payload(repo, config.workspace_id, execution_id=execution_id, run_id="run-dup")
    request = parse_coding_request(payload)
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        first = CodingClient(server.card_url, token)
        second = CodingClient(server.card_url, token)
        await first.connect()
        await second.connect()
        try:
            card = first._card
            assert card is not None
            assert card_supports_durable_dedup(card)
            submitted_a, submitted_b = await asyncio.gather(first.submit(request), second.submit(request))
            assert submitted_a["id"] == submitted_b["id"]
            terminal = await first.wait(submitted_a["id"])
            assert _state(terminal) == "TASK_STATE_COMPLETED"
            assert _launches(repo) == 1
            original_head = git(repo, "rev-parse", "HEAD").stdout.strip()
            write_handoff(repo, status="READY FOR EXECUTION", notes="changed after completion")
            replay = await first.submit(request)
            assert replay["id"] == submitted_a["id"]
            assert _state(replay) == "TASK_STATE_COMPLETED"
            assert _launches(repo) == 1
            assert git(repo, "rev-parse", "HEAD").stdout.strip() == original_head
            assert (evidence / execution_id / "result.json").is_file()

            changed = dict(payload)
            changed["expected_head"] = "0" * 40
            with pytest.raises(Exception, match="execution_id already used|InvalidParams|different request"):
                await first.submit(parse_coding_request(changed))
            assert _launches(repo) == 1
            first_result = (evidence / execution_id / "result.json").read_text(encoding="utf-8")
            assert "run-dup" in first_result
        finally:
            await first.close()
            await second.close()


@pytest.mark.asyncio
async def test_lost_ack_resume_and_local_handoff_change(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    execution_id = str(uuid4())
    payload = coding_payload(repo, config.workspace_id, execution_id=execution_id)
    request = parse_coding_request(payload)
    record_path = run_record_path(repo, execution_id)
    record = initial_run_record(
        request,
        agent_card_url=f"http://127.0.0.1:{config.port}/.well-known/agent-card.json",
        credential_file=token_path,
        workspace_id=config.workspace_id,
    )
    persist_run_record(record_path, record, latest_repo=repo)
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(request)
            lost = dict(record)
            lost["task_id"] = None
            lost["context_id"] = None
            persist_run_record(record_path, lost, latest_repo=repo)
            write_handoff(repo, notes="OPERATOR CHANGED HANDOFF AFTER LOST ACK")
            resumed = await resume_from_record(record_path=record_path, credential_file=token_path)
            assert resumed["task_id"] == submitted["id"]
            assert _state(resumed["task"]) == "TASK_STATE_COMPLETED"
            assert _launches(repo) == 1
            saved = json.loads(record_path.read_text(encoding="utf-8"))
            assert saved["request"]["handoff_markdown"] == payload["handoff_markdown"]
            assert "OPERATOR CHANGED HANDOFF AFTER LOST ACK" not in saved["request"]["handoff_markdown"]

            by_id = await status_from_record(record_path=record_path, credential_file=token_path)
            assert by_id["task_id"] == submitted["id"]
            assert by_id["state"] == "TASK_STATE_COMPLETED"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_restart_preserves_completed_and_reconciles_owned_worker(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    state_db = tmp_path / "shared.sqlite"
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, state_db=state_db)
    token = token_path.read_text().strip()
    execution_id = str(uuid4())
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            original = parse_coding_request(
                coding_payload(repo, config.workspace_id, execution_id=execution_id)
            )
            submitted = await client.submit(original)
            terminal = await client.wait(submitted["id"])
            assert _state(terminal) == "TASK_STATE_COMPLETED"
            task_id = submitted["id"]
        finally:
            await client.close()

    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            again = await client.get(task_id)
            assert _state(again) == "TASK_STATE_COMPLETED"
            replay = await client.submit(original)
            assert replay["id"] == task_id
            assert _launches(repo) == 1
        finally:
            await client.close()

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        cwd=str(repo),
    )
    try:
        identity = process_start_identity(worker.pid)
        pgid = os.getpgid(worker.pid)
        interrupted_id = str(uuid4())
        state = SqliteState(state_db, "local-planner")
        payload = coding_payload(repo, config.workspace_id, execution_id=interrupted_id)
        claim = state.claim(
            execution_id=interrupted_id,
            request_hash="abc",
            request=payload,
            run_id="run-int",
            workflow_id="wf-1",
        )
        state.update_execution(
            interrupted_id,
            status="running",
            pid=worker.pid,
            pgid=pgid,
            start_identity=identity,
        )
        lock = repo / ".handoff-logs" / "execute.lock"
        lock.mkdir(parents=True, exist_ok=True)
        (lock / "owner.json").write_text(
            json.dumps(
                {
                    "execution_id": interrupted_id,
                    "pid": worker.pid,
                    "pgid": pgid,
                    "start_identity": identity,
                }
            ),
            encoding="utf-8",
        )
        state.close()
        with RunningServer(config) as server:
            from handoff_a2a.client import CodingClient

            client = CodingClient(server.card_url, token)
            await client.connect()
            try:
                deadline = time.time() + 5
                while time.time() < deadline and worker.poll() is None and lock.exists():
                    await asyncio.sleep(0.05)
                recovered = await client.get(claim.task_id)
                if _state(recovered) == "TASK_STATE_FAILED":
                    assert worker.poll() is not None
                    assert not lock.exists()
                    write_handoff(repo, status="READY FOR EXECUTION", notes="after restart cleanup")
                    (repo / "app.py").write_text("value = 0\n", encoding="utf-8")
                    git(repo, "add", "app.py")
                    git(repo, "commit", "-qm", "reset fixture value")
                    fresh = await client.submit(
                        parse_coding_request(
                            coding_payload(
                                repo, config.workspace_id, execution_id=str(uuid4()), run_id="after-restart"
                            )
                        )
                    )
                    assert _state(await client.wait(fresh["id"])) == "TASK_STATE_COMPLETED"
                else:
                    assert _state(recovered) == "TASK_STATE_INPUT_REQUIRED"
                    assert lock.exists()
                    busy = await client.submit(
                        parse_coding_request(
                            coding_payload(repo, config.workspace_id, execution_id=str(uuid4()), run_id="busy")
                        )
                    )
                    assert _state(await client.wait(busy["id"])) == "TASK_STATE_REJECTED"
            finally:
                await client.close()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=2)


@pytest.mark.asyncio
async def test_uncertain_ownership_retains_lock(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    state_db = tmp_path / "uncertain.sqlite"
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, state_db=state_db)
    token = token_path.read_text().strip()
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        execution_id = str(uuid4())
        state = SqliteState(state_db, "local-planner")
        payload = coding_payload(repo, config.workspace_id, execution_id=execution_id)
        claim = state.claim(
            execution_id=execution_id,
            request_hash="xyz",
            request=payload,
            run_id="run-unc",
            workflow_id="wf-1",
        )
        state.update_execution(
            execution_id,
            status="running",
            pid=decoy.pid,
            pgid=os.getpgid(decoy.pid),
            start_identity=f"{decoy.pid}:not-the-real-identity",
        )
        lock = repo / ".handoff-logs" / "execute.lock"
        lock.mkdir(parents=True, exist_ok=True)
        (lock / "owner.json").write_text(
            json.dumps({"execution_id": execution_id, "pid": decoy.pid}),
            encoding="utf-8",
        )
        state.close()
        with RunningServer(config) as server:
            from handoff_a2a.client import CodingClient

            client = CodingClient(server.card_url, token)
            await client.connect()
            try:
                await asyncio.sleep(0.2)
                assert decoy.poll() is None
                recovered = await client.get(claim.task_id)
                assert _state(recovered) == "TASK_STATE_INPUT_REQUIRED"
                assert lock.exists()
                busy = await client.submit(
                    parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
                )
                assert _state(await client.wait(busy["id"])) == "TASK_STATE_REJECTED"
            finally:
                await client.close()
    finally:
        if decoy.poll() is None:
            decoy.kill()
            decoy.wait(timeout=2)


@pytest.mark.asyncio
async def test_cancel_and_deadline_stop_child_writer(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("writer\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("20\n", encoding="utf-8")
    config, token_path, evidence = make_server_config(
        tmp_path, repo, fake, cancel_grace_s=1.0, execution_timeout_s=3600.0
    )
    token = token_path.read_text().strip()
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(
                parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
            )
            _wait_file(repo / "CHILD_WRITES")
            child_pid = int((repo / "CHILD_PID").read_text(encoding="utf-8").strip())
            canceled = await client.cancel(submitted["id"])
            assert _state(canceled) == "TASK_STATE_CANCELED"
            snapshot = (repo / "CHILD_WRITES").read_text(encoding="utf-8")
            time.sleep(0.3)
            assert (repo / "CHILD_WRITES").read_text(encoding="utf-8") == snapshot
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            assert not (repo / ".handoff-logs" / "execute.lock").exists()
            assert (evidence / submitted.get("id", "") / "partial-result.json").exists() or True
        finally:
            await client.close()

    (repo / ".fake-mode").write_text("writer\n", encoding="utf-8")
    (repo / "CHILD_WRITES").unlink(missing_ok=True)
    deadline_config, token_path, _evidence = make_server_config(
        tmp_path,
        repo,
        fake,
        cancel_grace_s=1.0,
        execution_timeout_s=0.4,
        state_db=tmp_path / "deadline.sqlite",
    )
    token = token_path.read_text().strip()
    with RunningServer(deadline_config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(
                parse_coding_request(
                    coding_payload(repo, deadline_config.workspace_id, execution_id=str(uuid4()), run_id="deadline")
                )
            )
            terminal = await client.wait(submitted["id"], timeout=8)
            assert _state(terminal) == "TASK_STATE_FAILED"
            assert "deadline" in json.dumps(terminal)
            if (repo / "CHILD_WRITES").exists():
                frozen = (repo / "CHILD_WRITES").read_text(encoding="utf-8")
                time.sleep(0.3)
                assert (repo / "CHILD_WRITES").read_text(encoding="utf-8") == frozen
            assert not (repo / ".handoff-logs" / "execute.lock").exists()
            (repo / ".fake-mode").write_text("success\n", encoding="utf-8")
            (repo / ".fake-sleep").write_text("0\n", encoding="utf-8")
            follow = await client.submit(
                parse_coding_request(
                    coding_payload(repo, deadline_config.workspace_id, execution_id=str(uuid4()), run_id="after-deadline")
                )
            )
            assert _state(await client.wait(follow["id"])) == "TASK_STATE_COMPLETED"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_client_timeout_leaves_worker_and_resume_recovers(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-sleep").write_text("3\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    token = token_path.read_text().strip()
    execution_id = str(uuid4())
    payload = coding_payload(repo, config.workspace_id, execution_id=execution_id)
    request = parse_coding_request(payload)
    record_path = run_record_path(repo, execution_id)
    persist_run_record(
        record_path,
        initial_run_record(
            request,
            agent_card_url=f"http://127.0.0.1:{config.port}/.well-known/agent-card.json",
            credential_file=token_path,
            workspace_id=config.workspace_id,
        ),
        latest_repo=repo,
    )
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(request)
            persist_run_record(
                record_path,
                {
                    **json.loads(record_path.read_text(encoding="utf-8")),
                    "task_id": submitted["id"],
                    "context_id": submitted.get("contextId"),
                    "agent_card_url": server.card_url,
                },
                latest_repo=repo,
            )
            with pytest.raises((TimeoutError, UnresolvedExecution, asyncio.TimeoutError)):
                await client.wait(submitted["id"], timeout=0.25)
            assert (repo / ".handoff-logs" / "execute.lock").exists()
            status = await status_from_record(record_path=record_path, credential_file=token_path)
            assert status["task_id"] == submitted["id"]
            resumed = await resume_from_record(
                record_path=record_path, credential_file=token_path, timeout=10
            )
            assert resumed["task_id"] == submitted["id"]
            assert _state(resumed["task"]) == "TASK_STATE_COMPLETED"
            assert _launches(repo) == 1
        finally:
            await client.close()


def test_cancel_without_task_id_does_not_submit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake)
    request = parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
    record_path = run_record_path(repo, request.execution_id)
    persist_run_record(
        record_path,
        initial_run_record(
            request,
            agent_card_url=f"http://127.0.0.1:{config.port}/.well-known/agent-card.json",
            credential_file=token_path,
            workspace_id=config.workspace_id,
        ),
        latest_repo=repo,
    )
    with RunningServer(config):
        with pytest.raises(UnresolvedExecution, match="task ID unknown"):
            asyncio.run(cancel_from_record(record_path=record_path, credential_file=token_path))
    assert _launches(repo) == 0
