"""Provider-neutral A2A client for the coding-task profile."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.helpers.proto_helpers import new_data_message
from a2a.types.a2a_pb2 import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
)
from google.protobuf.json_format import MessageToDict, ParseDict

from handoff_a2a.contracts import (
    CODING_RESULT_ARTIFACT,
    CODING_TASK_PROFILE,
    DURABLE_DEDUP_PARAM,
    INTERRUPTED_TASK_STATES,
    CodingRequest,
    TERMINAL_TASK_STATES,
    identities_match,
    parse_coding_request,
    request_canonical_hash,
    snapshot_sha256,
)
from handoff_a2a.workspace import HANDOFF_NAME, LOG_DIRNAME, parse_handoff

AGENT_CARD_PATH = "/.well-known/agent-card.json"
RUN_RECORD_SCHEMA = "urn:handoff-automation:coding-task:v1#run-record"
RUNS_DIRNAME = "a2a-runs"
LATEST_POINTER = "a2a-latest.json"


class ClientError(Exception):
    pass


class UnresolvedExecution(ClientError):
    def __init__(self, message: str, *, task_id: str | None, execution_id: str):
        super().__init__(message)
        self.task_id = task_id
        self.execution_id = execution_id


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def normalize_agent_card_url(url: str) -> str:
    trimmed = url.rstrip("/")
    if trimmed.endswith(AGENT_CARD_PATH):
        return trimmed
    return trimmed + AGENT_CARD_PATH


def verify_agent_card(card: AgentCard, card_url: str) -> str:
    if not any(ext.uri == CODING_TASK_PROFILE for ext in card.capabilities.extensions):
        raise ClientError("agent card does not advertise the coding-task profile")
    interface = next(
        (
            item
            for item in card.supported_interfaces
            if item.protocol_binding == "JSONRPC" and item.protocol_version == "1.0"
        ),
        None,
    )
    if interface is None:
        raise ClientError("agent card has no JSONRPC 1.0 interface")
    if _origin(interface.url) != _origin(card_url):
        raise ClientError("agent card JSONRPC interface is not the same origin as the card URL")
    return interface.url


SUCCESS_TASK_STATES = frozenset({"TASK_STATE_COMPLETED", "COMPLETED"})
UNSUCCESSFUL_TASK_STATES = frozenset(
    {
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_CANCELED",
        "FAILED",
        "REJECTED",
        "CANCELED",
        "CANCELLED",
    }
)


def coding_result_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") != CODING_RESULT_ARTIFACT:
            continue
        for part in artifact.get("parts") or []:
            data = part.get("data")
            if isinstance(data, dict):
                return data
    return None


def _data_parts_from_message(message: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(message, Mapping):
        return []
    found: list[dict[str, Any]] = []
    for part in message.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        data = part.get("data")
        if isinstance(data, dict):
            found.append(data)
    return found


def task_reason(task: Mapping[str, Any] | None) -> str | None:
    """Failure/rejection reason from a coding-result artifact or status message."""
    if not task:
        return None
    result = coding_result_from_task(dict(task))
    if result:
        reason = result.get("reason")
        if reason:
            return str(reason)
    status = task.get("status") if isinstance(task.get("status"), Mapping) else {}
    for data in _data_parts_from_message(status.get("message") if isinstance(status, Mapping) else None):
        if data.get("reason"):
            return str(data["reason"])
    return None


def execute_exit_code(state: str | None) -> int:
    """0 COMPLETED, 1 unsuccessful terminal, 2 unresolved/interrupted."""
    if state in SUCCESS_TASK_STATES:
        return 0
    if state in UNSUCCESSFUL_TASK_STATES:
        return 1
    return 2


def card_supports_durable_dedup(card: AgentCard) -> bool:
    for ext in card.capabilities.extensions:
        if ext.uri != CODING_TASK_PROFILE:
            continue
        params = MessageToDict(ext.params) if ext.HasField("params") else {}
        return bool(params.get(DURABLE_DEDUP_PARAM))
    return False


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def run_record_path(repo: Path, execution_id: str) -> Path:
    return repo / LOG_DIRNAME / RUNS_DIRNAME / f"{execution_id}.json"


def load_run_record(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ClientError("run record must be a JSON object")
    return data


def persist_run_record(path: Path, payload: Mapping[str, Any], *, latest_repo: Path | None = None) -> Path:
    write_json_atomic(path, payload)
    if latest_repo is not None:
        write_json_atomic(
            latest_repo / LOG_DIRNAME / LATEST_POINTER,
            {"run_record": str(path), "execution_id": payload.get("execution_id")},
        )
    return path


def initial_run_record(
    request: CodingRequest,
    *,
    agent_card_url: str,
    credential_file: Path | None,
    workspace_id: str,
) -> dict[str, Any]:
    now = _run_stamp()
    return {
        "schema": RUN_RECORD_SCHEMA,
        "caller_id": "local-planner",
        "execution_id": request.execution_id,
        "run_id": request.run_id,
        "workflow_id": request.workflow_id,
        "iteration": request.iteration,
        "workspace_id": workspace_id,
        "agent_card_url": agent_card_url,
        "credential_file": str(credential_file) if credential_file else None,
        "request": request.to_dict(),
        "request_hash": request_canonical_hash(request),
        "task_id": None,
        "context_id": None,
        "created_at": now,
        "updated_at": now,
        "last_task_state": None,
    }


def request_from_repo(
    repo: Path,
    workspace_id: str,
    *,
    workflow_id: str | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
    iteration: int | None = None,
) -> CodingRequest:
    repo = repo.resolve()
    markdown = (repo / HANDOFF_NAME).read_text(encoding="utf-8")
    document = parse_handoff(markdown)
    branch = _git(repo, "branch", "--show-current")
    head = _git(repo, "rev-parse", "HEAD")
    meta_path = repo / LOG_DIRNAME / "a2a-client.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if workflow_id is None:
        if document.status == "CHANGES REQUESTED" and meta.get("workflow_id"):
            workflow_id = str(meta["workflow_id"])
        else:
            workflow_id = str(uuid.uuid4())
    if iteration is None:
        if document.status == "CHANGES REQUESTED":
            iteration = int(meta.get("last_iteration") or 1) + 1
        else:
            iteration = 1
    return parse_coding_request(
        {
            "schema": CODING_TASK_PROFILE,
            "workflow_id": workflow_id,
            "run_id": run_id or f"{_run_stamp()}-{os.getpid()}",
            "execution_id": execution_id or str(uuid.uuid4()),
            "iteration": iteration,
            "workspace_id": workspace_id,
            "expected_branch": branch,
            "expected_head": head,
            "handoff_markdown": markdown,
            "request_sha256": snapshot_sha256(markdown),
        }
    )


def persist_client_metadata(repo: Path, request: CodingRequest, task: dict[str, Any]) -> Path:
    log_dir = repo / LOG_DIRNAME
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "a2a-client.json"
    payload = {
        "workflow_id": request.workflow_id,
        "run_id": request.run_id,
        "execution_id": request.execution_id,
        "iteration": request.iteration,
        "last_iteration": request.iteration,
        "task_id": task.get("id"),
        "context_id": task.get("contextId") or task.get("context_id"),
        "status": (task.get("status") or {}).get("state"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (log_dir / f"a2a-{request.run_id}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _run_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _git(repo: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class CodingClient:
    agent_card_url: str
    credential: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.agent_card_url = normalize_agent_card_url(self.agent_card_url)
        self._card: AgentCard | None = None
        self._transport: JsonRpcTransport | None = None
        self._http: httpx.AsyncClient | None = None
        self._rpc_url: str | None = None

    async def connect(self) -> dict[str, Any]:
        """Fetch and verify the Agent Card before sending credentials or a snapshot."""
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as anonymous:
            response = await anonymous.get(self.agent_card_url)
            response.raise_for_status()
            card_json = response.json()
        card = ParseDict(card_json, AgentCard())
        rpc_url = verify_agent_card(card, self.agent_card_url)
        self._card = card
        self._rpc_url = rpc_url
        self._http = httpx.AsyncClient(
            timeout=self.timeout,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {self.credential}",
                "A2A-Version": "1.0",
                "A2A-Extensions": CODING_TASK_PROFILE,
            },
        )
        self._transport = JsonRpcTransport(self._http, card, rpc_url)
        return card_json

    def _require_transport(self) -> JsonRpcTransport:
        if self._transport is None:
            raise ClientError("connect() before submit/get/wait")
        return self._transport

    def supports_durable_dedup(self) -> bool:
        if self._card is None:
            raise ClientError("connect() before checking durable deduplication")
        return card_supports_durable_dedup(self._card)

    def rpc_url(self) -> str | None:
        return self._rpc_url

    async def submit(self, request: CodingRequest) -> dict[str, Any]:
        transport = self._require_transport()
        message = new_data_message(
            request.to_dict(),
            role=Role.ROLE_USER,
            media_type="application/json",
        )
        rpc_request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        response = await transport.send_message(rpc_request)
        if not response.HasField("task"):
            raise ClientError("coding profile requires a Task response")
        return MessageToDict(response.task)

    async def get(self, task_id: str) -> dict[str, Any]:
        transport = self._require_transport()
        task = await transport.get_task(GetTaskRequest(id=task_id))
        return MessageToDict(task)

    async def wait(self, task_id: str, timeout: float = 180.0, interval: float = 0.2) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                task = await self.get(task_id)
                state = (task.get("status") or {}).get("state")
                if state in TERMINAL_TASK_STATES or state in INTERRUPTED_TASK_STATES:
                    return task
                await asyncio.sleep(interval)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        transport = self._require_transport()
        task = await transport.cancel_task(CancelTaskRequest(id=task_id))
        return MessageToDict(task)

    async def execute(self, request: CodingRequest, timeout: float = 180.0) -> dict[str, Any]:
        task_id = None
        try:
            if self._transport is None:
                await self.connect()
            task = await self.submit(request)
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ClientError("submit returned a task without an id")
            terminal = await self.wait(task_id, timeout=timeout)
            result = coding_result_from_task(terminal)
            if result is not None and not identities_match(request, result, task_id):
                raise ClientError("returned run/execution/task identities do not match the request")
            return terminal
        except ClientError:
            raise
        except Exception as exc:
            raise UnresolvedExecution(
                f"execution unresolved: {exc}",
                task_id=task_id,
                execution_id=request.execution_id,
            ) from exc

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            self._transport = None


def _update_record_from_task(record: dict[str, Any], task: Mapping[str, Any] | None) -> dict[str, Any]:
    updated = dict(record)
    updated["updated_at"] = _run_stamp()
    if task:
        updated["task_id"] = task.get("id") or updated.get("task_id")
        updated["context_id"] = task.get("contextId") or task.get("context_id") or updated.get("context_id")
        updated["last_task_state"] = (task.get("status") or {}).get("state")
    return updated


async def execute_repo(
    *,
    repo: Path,
    agent_card_url: str,
    credential_file: Path,
    workspace_id: str,
    timeout: float = 180.0,
) -> dict[str, Any]:
    credential = credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise ClientError("credential-file is empty")
    request = request_from_repo(repo, workspace_id)
    record_path = run_record_path(repo, request.execution_id)
    record = initial_run_record(
        request,
        agent_card_url=normalize_agent_card_url(agent_card_url),
        credential_file=credential_file,
        workspace_id=workspace_id,
    )
    persist_run_record(record_path, record, latest_repo=repo)
    client = CodingClient(agent_card_url, credential)
    try:
        if client._transport is None:
            await client.connect()
        record["agent_card_url"] = client.agent_card_url
        persist_run_record(record_path, record, latest_repo=repo)
        task = await client.submit(request)
        record = _update_record_from_task(record, task)
        persist_run_record(record_path, record, latest_repo=repo)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ClientError("submit returned a task without an id")
        terminal = await client.wait(task_id, timeout=timeout)
        result = coding_result_from_task(terminal)
        if result is not None and not identities_match(request, result, task_id):
            raise ClientError("returned run/execution/task identities do not match the request")
        persist_client_metadata(repo, request, terminal)
        record = _update_record_from_task(record, terminal)
        persist_run_record(record_path, record, latest_repo=repo)
        return {
            "request": request.to_dict(),
            "task": terminal,
            "result": result,
            "task_id": terminal.get("id"),
            "execution_id": request.execution_id,
            "reason": task_reason(terminal),
            "local_metadata": str(repo / LOG_DIRNAME / "a2a-client.json"),
            "run_record": str(record_path),
        }
    except UnresolvedExecution as exc:
        persist_client_metadata(
            repo,
            request,
            {"id": exc.task_id, "contextId": None, "status": {"state": "UNRESOLVED"}},
        )
        record = _update_record_from_task(record, {"id": exc.task_id, "status": {"state": "UNRESOLVED"}})
        persist_run_record(record_path, record, latest_repo=repo)
        raise
    except Exception as exc:
        persist_run_record(record_path, record, latest_repo=repo)
        if isinstance(exc, ClientError):
            raise
        raise UnresolvedExecution(
            f"execution unresolved: {exc}",
            task_id=record.get("task_id") if isinstance(record.get("task_id"), str) else None,
            execution_id=request.execution_id,
        ) from exc
    finally:
        await client.close()


async def status_from_record(*, record_path: Path, credential_file: Path, timeout: float = 30.0) -> dict[str, Any]:
    record = load_run_record(record_path)
    credential = credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise ClientError("credential-file is empty")
    task_id = record.get("task_id")
    if not task_id:
        return {
            "run_record": str(record_path),
            "execution_id": record.get("execution_id"),
            "task_id": None,
            "state": None,
            "unresolved": True,
            "reason": "submission acknowledgement was never saved; resume to retransmit the saved request",
        }
    client = CodingClient(str(record.get("agent_card_url") or ""), credential, timeout=timeout)
    try:
        await client.connect()
        task = await client.get(str(task_id))
        record = _update_record_from_task(record, task)
        persist_run_record(record_path, record)
        result = coding_result_from_task(task)
        return {
            "run_record": str(record_path),
            "execution_id": record.get("execution_id"),
            "task_id": task.get("id"),
            "state": (task.get("status") or {}).get("state"),
            "reason": task_reason(task),
            "recovery_required": _recovery_required(task),
            "result": result,
            "unresolved": False,
            "task": task,
        }
    except Exception as exc:
        raise UnresolvedExecution(
            f"status unresolved: {exc}",
            task_id=str(task_id),
            execution_id=str(record.get("execution_id") or ""),
        ) from exc
    finally:
        await client.close()


async def resume_from_record(*, record_path: Path, credential_file: Path, timeout: float = 180.0) -> dict[str, Any]:
    record = load_run_record(record_path)
    credential = credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise ClientError("credential-file is empty")
    request = parse_coding_request(record.get("request") or {})
    saved_card = str(record.get("agent_card_url") or "")
    client = CodingClient(saved_card, credential, timeout=min(timeout, 30.0))
    try:
        await client.connect()
        if client.agent_card_url.rstrip("/") != normalize_agent_card_url(saved_card).rstrip("/"):
            raise ClientError("saved endpoint identity does not match the connected Agent Card URL")
        task_id = record.get("task_id")
        if task_id:
            task = await client.get(str(task_id))
            state = (task.get("status") or {}).get("state")
            if state not in TERMINAL_TASK_STATES and state not in INTERRUPTED_TASK_STATES:
                task = await client.wait(str(task_id), timeout=timeout)
        else:
            if not client.supports_durable_dedup():
                raise ClientError(
                    "endpoint does not advertise durable_execution_id_deduplication; refusing retransmission"
                )
            task = await client.submit(request)
            record = _update_record_from_task(record, task)
            persist_run_record(record_path, record)
            wait_id = task.get("id")
            if not isinstance(wait_id, str) or not wait_id:
                raise ClientError("submit returned a task without an id")
            task = await client.wait(wait_id, timeout=timeout)
        record = _update_record_from_task(record, task)
        persist_run_record(record_path, record)
        result = coding_result_from_task(task)
        return {
            "run_record": str(record_path),
            "request": request.to_dict(),
            "task": task,
            "result": result,
            "task_id": task.get("id"),
            "execution_id": request.execution_id,
            "reason": task_reason(task),
            "recovery_required": _recovery_required(task),
        }
    except UnresolvedExecution:
        raise
    except ClientError:
        raise
    except Exception as exc:
        raise UnresolvedExecution(
            f"resume unresolved: {exc}",
            task_id=record.get("task_id") if isinstance(record.get("task_id"), str) else None,
            execution_id=request.execution_id,
        ) from exc
    finally:
        await client.close()


async def cancel_from_record(*, record_path: Path, credential_file: Path, timeout: float = 30.0) -> dict[str, Any]:
    record = load_run_record(record_path)
    credential = credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise ClientError("credential-file is empty")
    task_id = record.get("task_id")
    if not task_id:
        raise UnresolvedExecution(
            "task ID unknown; resume/reconcile before cancel so a new execution is not created",
            task_id=None,
            execution_id=str(record.get("execution_id") or ""),
        )
    client = CodingClient(str(record.get("agent_card_url") or ""), credential, timeout=timeout)
    try:
        await client.connect()
        task = await client.cancel(str(task_id))
        record = _update_record_from_task(record, task)
        persist_run_record(record_path, record)
        return {
            "run_record": str(record_path),
            "execution_id": record.get("execution_id"),
            "task_id": task.get("id"),
            "state": (task.get("status") or {}).get("state"),
            "reason": task_reason(task),
            "recovery_required": _recovery_required(task),
            "task": task,
        }
    except UnresolvedExecution:
        raise
    except Exception as exc:
        raise UnresolvedExecution(
            f"cancel unresolved: {exc}",
            task_id=str(task_id),
            execution_id=str(record.get("execution_id") or ""),
        ) from exc
    finally:
        await client.close()


def _recovery_required(task: Mapping[str, Any]) -> bool:
    for data in _data_parts_from_message((task.get("status") or {}).get("message")):
        if data.get("recovery_required"):
            return True
    return False
