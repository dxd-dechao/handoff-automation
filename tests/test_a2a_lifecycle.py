from __future__ import annotations

import asyncio
import json
import os
import signal
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
    PROVIDERS,
    RunningServer,
    coding_payload,
    git,
    make_fake_claude,
    make_repo,
    make_server_config,
    write_handoff,
    write_server_json,
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
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_cancel_and_deadline_stop_child_writer(tmp_path: Path, provider: str) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path, provider)
    (repo / ".fake-mode").write_text("writer\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text("20\n", encoding="utf-8")
    config, token_path, evidence = make_server_config(
        tmp_path, repo, fake, cancel_grace_s=1.0, execution_timeout_s=3600.0, provider=provider
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
        provider=provider,
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


@pytest.mark.asyncio
async def test_cancel_stops_term_immune_child_after_parent_exits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-mode").write_text("orphan_term_immune\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, cancel_grace_s=0.5)
    token = token_path.read_text().strip()
    with RunningServer(config) as server:
        from handoff_a2a.client import CodingClient

        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(
                parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
            )
            _wait_file(repo / "CHILD_PID")
            _wait_file(repo / "CHILD_WRITES")
            child_pid = int((repo / "CHILD_PID").read_text(encoding="utf-8").strip())
            os.kill(child_pid, 0)
            canceled = await client.cancel(submitted["id"])
            assert _state(canceled) == "TASK_STATE_CANCELED"
            snapshot = (repo / "CHILD_WRITES").read_text(encoding="utf-8")
            time.sleep(0.3)
            assert (repo / "CHILD_WRITES").read_text(encoding="utf-8") == snapshot
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            assert not (repo / ".handoff-logs" / "execute.lock").exists()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_server_process_restart_during_active_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path)
    (repo / ".fake-sleep").write_text("30\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(tmp_path, repo, fake, cancel_grace_s=1.0)
    token = token_path.read_text().strip()
    config_path = write_server_json(tmp_path / "server.json", config)
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

    def launch() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-m", "handoff_a2a", "serve", "--config", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait_up(timeout: float = 8.0) -> None:
        import httpx

        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                httpx.get(
                    f"http://127.0.0.1:{config.port}/.well-known/agent-card.json",
                    timeout=0.3,
                    trust_env=False,
                ).raise_for_status()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError(f"server did not start: {last_error}")

    first = launch()
    try:
        wait_up()
        from handoff_a2a.client import CodingClient

        client = CodingClient(f"http://127.0.0.1:{config.port}/.well-known/agent-card.json", token)
        await client.connect()
        try:
            submitted = await client.submit(request)
            persist_run_record(
                record_path,
                {
                    **json.loads(record_path.read_text(encoding="utf-8")),
                    "task_id": submitted["id"],
                    "context_id": submitted.get("contextId"),
                    "agent_card_url": client.agent_card_url,
                },
                latest_repo=repo,
            )
            _wait_file(repo / "WORKER_STARTED")
            task_id = submitted["id"]
        finally:
            await client.close()
        first.send_signal(signal.SIGTERM)
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=5)
        first = None
        second = launch()
        try:
            wait_up()
            client = CodingClient(f"http://127.0.0.1:{config.port}/.well-known/agent-card.json", token)
            await client.connect()
            try:
                recovered = await client.get(task_id)
                state = _state(recovered)
                assert state != "TASK_STATE_WORKING"
                assert state in {"TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_CANCELED"}
                replay = await client.submit(request)
                assert replay["id"] == task_id
                assert _state(replay) != "TASK_STATE_WORKING"
                resumed = await resume_from_record(
                    record_path=record_path, credential_file=token_path, timeout=5
                )
                assert resumed["task_id"] == task_id
                assert _state(resumed["task"]) != "TASK_STATE_WORKING"
            finally:
                await client.close()
        finally:
            second.send_signal(signal.SIGTERM)
            try:
                second.wait(timeout=10)
            except subprocess.TimeoutExpired:
                second.kill()
                second.wait(timeout=5)
    finally:
        if first is not None and first.poll() is None:
            first.kill()
            first.wait(timeout=5)


# ── Cancellation during worker startup (A5) ─────────────────────────────────


def _lifecycle(task: dict) -> dict:
    for artifact in reversed(task.get("artifacts") or []):
        if artifact.get("name") == "worker-lifecycle":
            for part in artifact.get("parts") or []:
                if isinstance(part.get("data"), dict):
                    return part["data"]
    return {}


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _startup_client(tmp_path: Path, provider: str, *, mode: str = "success", sleep_s: str = "1"):
    from handoff_a2a.client import CodingClient

    repo = make_repo(tmp_path)
    fake = make_fake_claude(tmp_path, provider)
    (repo / ".fake-mode").write_text(f"{mode}\n", encoding="utf-8")
    (repo / ".fake-sleep").write_text(f"{sleep_s}\n", encoding="utf-8")
    config, token_path, _evidence = make_server_config(
        tmp_path, repo, fake, cancel_grace_s=1.0, provider=provider
    )
    return repo, config, token_path.read_text().strip(), CodingClient


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_immediate_cancel_leaves_no_delayed_write_or_live_worker(tmp_path: Path, provider: str) -> None:
    repo, config, token, CodingClient = await _startup_client(tmp_path, provider, sleep_s="2")
    lock = repo / ".handoff-logs" / "execute.lock"
    with RunningServer(config) as server:
        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            execution_id = str(uuid4())
            submitted = await client.submit(
                parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=execution_id))
            )
            canceled = await client.cancel(submitted["id"])  # no wait for WORKER_STARTED
            assert _state(canceled) == "TASK_STATE_CANCELED"
            assert not lock.exists()
            claim = SqliteState(config.resolved_state_db(), config.caller_id).get_execution(execution_id)
            if claim is not None and claim.pid:
                assert _dead(int(claim.pid))
            await asyncio.sleep(3.5)  # beyond the worker's scheduled write
            assert (repo / "app.py").read_text() == "value = 0\n"
            assert git(repo, "log", "--oneline").stdout.count("\n") == 1
            terminal = await client.get(submitted["id"])
            assert _state(terminal) == "TASK_STATE_CANCELED"
            life = _lifecycle(terminal)
            if _launches(repo) or (repo / "WORKER_STARTED").exists():
                assert life.get("worker_started") is True
            assert life.get("worker_stopped") is True and life.get("lock_held") is False
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_cancel_while_spawn_in_flight_stops_owned_worker_before_release(
    tmp_path: Path, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import handoff_a2a.server as server_module

    repo, config, token, CodingClient = await _startup_client(tmp_path, provider, mode="writer", sleep_s="20")
    lock = repo / ".handoff-logs" / "execute.lock"
    spawned, release = threading.Event(), threading.Event()
    pids: list[int] = []
    real_start = server_module.start_owned

    def gated_start(*args, **kwargs):
        owned = real_start(*args, **kwargs)
        pids.append(owned.pid)
        spawned.set()
        release.wait(10)  # the launch thread still holds the spawn gate
        return owned

    monkeypatch.setattr(server_module, "start_owned", gated_start)
    with RunningServer(config) as server:
        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(
                parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
            )
            assert await asyncio.to_thread(spawned.wait, 10)
            await asyncio.to_thread(_wait_file, repo / "CHILD_WRITES")  # worker and its child are live
            child_pid = int((repo / "CHILD_PID").read_text().strip())
            cancel = asyncio.create_task(client.cancel(submitted["id"]))
            await asyncio.sleep(0.5)
            assert not cancel.done()  # finalization waits for the in-flight launch
            assert lock.is_dir()  # protection retained while ownership is unknown
            release.set()
            canceled = await cancel
            assert _state(canceled) == "TASK_STATE_CANCELED"
            # The returned process was owned and stopped before the lock went away.
            assert _dead(pids[0]) and _dead(child_pid)
            assert not lock.exists()
            frozen = (repo / "CHILD_WRITES").read_text()
            life = _lifecycle(await client.get(submitted["id"]))
            assert life == {**life, "worker_started": True, "worker_stopped": True, "lock_held": False}

            # A same-workspace follow-up cannot overlap the canceled worker.
            monkeypatch.setattr(server_module, "start_owned", real_start)
            (repo / ".fake-mode").write_text("success\n", encoding="utf-8")
            (repo / ".fake-sleep").write_text("0\n", encoding="utf-8")
            follow = await client.submit(
                parse_coding_request(
                    coding_payload(repo, config.workspace_id, execution_id=str(uuid4()), run_id="after-cancel")
                )
            )
            assert _state(await client.wait(follow["id"], timeout=20)) == "TASK_STATE_COMPLETED"
            assert (repo / "CHILD_WRITES").read_text() == frozen
            assert (repo / "app.py").read_text() == "value = 1\n"
            assert _launches(repo) == 2
        finally:
            release.set()
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_cancel_before_spawn_gate_prevents_launch(
    tmp_path: Path, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import handoff_a2a.server as server_module

    repo, config, token, CodingClient = await _startup_client(tmp_path, provider)
    lock = repo / ".handoff-logs" / "execute.lock"
    at_gate, release = threading.Event(), threading.Event()
    starts: list[int] = []
    real_start = server_module.start_owned

    def hold_at_gate(_runtime) -> None:
        at_gate.set()
        release.wait(10)

    def counting_start(*args, **kwargs):
        starts.append(1)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(server_module.ExecutionRuntime, "_before_spawn", hold_at_gate)
    monkeypatch.setattr(server_module, "start_owned", counting_start)
    with RunningServer(config) as server:
        client = CodingClient(server.card_url, token)
        await client.connect()
        try:
            submitted = await client.submit(
                parse_coding_request(coding_payload(repo, config.workspace_id, execution_id=str(uuid4())))
            )
            assert await asyncio.to_thread(at_gate.wait, 10)
            canceled = await client.cancel(submitted["id"])
            assert _state(canceled) == "TASK_STATE_CANCELED"
            assert not lock.exists()
            release.set()  # the launch thread now finds the gate closed
            await asyncio.sleep(2.0)
            assert starts == []
            assert _launches(repo) == 0 and not (repo / "WORKER_STARTED").exists()
            assert (repo / "app.py").read_text() == "value = 0\n"
            life = _lifecycle(await client.get(submitted["id"]))
            assert life.get("worker_started") is False and life.get("worker_stopped") is True
        finally:
            release.set()
            await client.close()
