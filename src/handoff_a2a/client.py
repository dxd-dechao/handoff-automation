"""Provider-neutral A2A client for the coding-task profile."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.helpers.proto_helpers import new_data_message
from a2a.types.a2a_pb2 import (
    AgentCard,
    GetTaskRequest,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
)
from google.protobuf.json_format import MessageToDict, ParseDict

from handoff_a2a.contracts import (
    CODING_RESULT_ARTIFACT,
    CODING_TASK_PROFILE,
    CodingRequest,
    TERMINAL_TASK_STATES,
    identities_match,
    parse_coding_request,
    snapshot_sha256,
)
from handoff_a2a.workspace import HANDOFF_NAME, LOG_DIRNAME, parse_handoff

AGENT_CARD_PATH = "/.well-known/agent-card.json"


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


def coding_result_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") != CODING_RESULT_ARTIFACT:
            continue
        for part in artifact.get("parts") or []:
            data = part.get("data")
            if isinstance(data, dict):
                return data
    return None


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
                if state in TERMINAL_TASK_STATES:
                    return task
                await asyncio.sleep(interval)

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
    client = CodingClient(agent_card_url, credential)
    try:
        task = await client.execute(request, timeout=timeout)
        persist_client_metadata(repo, request, task)
        return {
            "request": request.to_dict(),
            "task": task,
            "result": coding_result_from_task(task),
            "task_id": task.get("id"),
            "execution_id": request.execution_id,
            "local_metadata": str(repo / LOG_DIRNAME / "a2a-client.json"),
        }
    except UnresolvedExecution as exc:
        persist_client_metadata(
            repo,
            request,
            {"id": exc.task_id, "contextId": None, "status": {"state": "UNRESOLVED"}},
        )
        raise
    finally:
        await client.close()
