"""Loopback A2A server: Agent Card, JSON-RPC, durable claims, owned workers."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_part,
    new_task_from_user_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.tasks import TaskUpdater
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Message,
    Role,
    SecurityScheme,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import InvalidParamsError, TaskNotCancelableError
from google.protobuf.struct_pb2 import Struct
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from handoff_a2a.adapters.claude import (
    ClaudeAdapter,
    ClaudeAdapterConfig,
    child_environment,
    interpret_claude_files,
)
from handoff_a2a.contracts import (
    CODING_RESULT_ARTIFACT,
    CODING_TASK_PROFILE,
    DURABLE_DEDUP_PARAM,
    WORKER_LIFECYCLE_ARTIFACT,
    WORKSPACE_FINGERPRINT_PARAM,
    CodingRequest,
    CodingResult,
    CommitRef,
    ContractError,
    parse_coding_request,
    request_canonical_hash,
)
from handoff_a2a.processes import (
    OwnedProcess,
    identity_matches,
    is_owned_alive,
    owned_from_record,
    pid_exists,
    start_owned,
    stop_owned,
    wait_owned,
)
from handoff_a2a.store import ClaimConflict, ExecutionClaim, SqliteState, SqliteTaskStore
from handoff_a2a.workspace import (
    GitWorkspace,
    WorkspaceBusy,
    WorkspaceError,
    release_lock_if_owner,
)

LOGGER = logging.getLogger("handoff_a2a.server")
RPC_PATH = "/a2a"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TERMINAL_EXECUTION = frozenset({"completed", "failed", "rejected", "canceled"})
TERMINAL_TASK_PROTO = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_REJECTED,
        TaskState.TASK_STATE_CANCELED,
    }
)
EXECUTION_TO_TASK_STATE = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "rejected": TaskState.TASK_STATE_REJECTED,
    "canceled": TaskState.TASK_STATE_CANCELED,
}


def persist_task_outcome(
    store: SqliteState,
    *,
    task_id: str,
    context_id: str,
    state: TaskState,
    payload: dict[str, Any],
) -> None:
    task = store.load_task(task_id)
    if task is None:
        task = Task(id=task_id, context_id=context_id)
    if task.status.state in TERMINAL_TASK_PROTO:
        return
    task.status.CopyFrom(
        TaskStatus(
            state=state,
            message=Message(
                role=Role.ROLE_AGENT,
                message_id=str(uuid.uuid4()),
                task_id=task_id,
                context_id=context_id,
                parts=[new_data_part(payload)],
            ),
        )
    )
    store.save_task(task)


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    workspace_id: str
    workspace_path: Path
    credential_file: Path
    evidence_dir: Path
    claude: ClaudeAdapterConfig
    public_base_url: str
    credential: str
    state_db: Path | None = None
    caller_id: str = "local-planner"
    execution_timeout_s: float = 3600.0
    cancel_grace_s: float = 5.0

    def resolved_state_db(self) -> Path:
        return Path(self.state_db) if self.state_db is not None else self.evidence_dir / "state.sqlite"


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def _positive_number(raw: Any, name: str, default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a positive number")
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def load_server_config(path: Path) -> ServerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("server config must be a JSON object")
    host = str(raw.get("host") or "")
    if not _is_loopback_host(host):
        raise ValueError(
            f"host {host!r} is not loopback-only; A2 binds 127.0.0.1, ::1, or localhost"
        )
    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise ValueError("port must be an integer 1-65535")
    claude_raw = raw.get("claude")
    if not isinstance(claude_raw, dict):
        raise ValueError("claude adapter config is required")
    binary = str(claude_raw.get("binary") or "")
    model = str(claude_raw.get("model") or "")
    if not binary or not model:
        raise ValueError("claude.binary and claude.model are required")
    credential_file = Path(str(raw.get("credential_file") or "")).expanduser()
    if not credential_file.is_file():
        raise ValueError(f"credential-file not found: {credential_file}")
    credential = credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise ValueError("credential-file is empty")
    workspace_id = str(raw.get("workspace_id") or "")
    workspace_path = Path(str(raw.get("workspace_path") or "")).expanduser()
    evidence_dir = Path(str(raw.get("evidence_dir") or "")).expanduser()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if not workspace_path:
        raise ValueError("workspace_path is required")
    if not evidence_dir:
        raise ValueError("evidence_dir is required")
    public = str(raw.get("public_base_url") or "").rstrip("/")
    if not public:
        hostname = f"[{host}]" if ":" in host and host != "localhost" else host
        public = f"http://{hostname}:{port}"
    state_db_raw = raw.get("state_db")
    caller_id = str(raw.get("caller_id") or "local-planner").strip() or "local-planner"
    return ServerConfig(
        host=host,
        port=port,
        workspace_id=workspace_id,
        workspace_path=workspace_path.resolve(),
        credential_file=credential_file.resolve(),
        evidence_dir=evidence_dir.resolve(),
        claude=ClaudeAdapterConfig(binary=binary, model=model),
        public_base_url=public,
        credential=credential,
        state_db=Path(str(state_db_raw)).expanduser().resolve() if state_db_raw else None,
        caller_id=caller_id,
        execution_timeout_s=_positive_number(raw.get("execution_timeout_s"), "execution_timeout_s", 3600.0),
        cancel_grace_s=_positive_number(raw.get("cancel_grace_s"), "cancel_grace_s", 5.0),
    )


def _extension_params() -> Struct:
    params = Struct()
    params.update(
        {DURABLE_DEDUP_PARAM: True, WORKSPACE_FINGERPRINT_PARAM: True}
    )
    return params


def build_agent_card(config: ServerConfig) -> AgentCard:
    rpc_url = f"{config.public_base_url}{RPC_PATH}"
    return AgentCard(
        name="handoff-a2a Claude Executor",
        description=(
            "Experimental local coding Executor. Tasks persist in a local SQLite "
            "store. Duplicate execution_id submissions reuse the original task. "
            "A COMPLETED task is ready for independent Planner QA, not APPROVED."
        ),
        version="0.2.0",
        supported_interfaces=[
            AgentInterface(
                url=rpc_url,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[
                AgentExtension(
                    uri=CODING_TASK_PROFILE,
                    required=True,
                    description="HANDOFF.md snapshot coding-task profile v1",
                    params=_extension_params(),
                )
            ],
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="implement-approved-coding-task",
                name="Implement approved coding task",
                description=(
                    "Implement the submitted HANDOFF.md snapshot in the registered "
                    "local git workspace using the fixed ritual prompt."
                ),
                tags=["coding", "handoff"],
            )
        ],
        security_schemes={
            "localBearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[{"schemes": {"localBearer": {"list": []}}}],
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _extract_coding_request(params: SendMessageRequest) -> CodingRequest:
    parts = get_data_parts(params.message.parts)
    if not parts:
        raise ContractError("SendMessage must carry a coding-task data part")
    return parse_coding_request(parts[0])


class ExecutionRuntime:
    """Per-execution cleanup owner. Outlives HTTP/SDK producer cancellation."""

    def __init__(self, executor: CodingAgentExecutor, request: CodingRequest, task_id: str, context_id: str):
        self.executor = executor
        self.request = request
        self.task_id = task_id
        self.context_id = context_id
        self.owned: OwnedProcess | None = None
        self.lock_acquired = False
        self.worker_launched = False
        self.evidence = executor.config.evidence_dir / request.execution_id
        self.before_git = None
        self.before_doc = None
        self._cleanup = asyncio.Lock()
        self._finalized = False
        self._closing = False
        self._final_kind: str | None = None
        self.started_at = time.monotonic()

    async def stop_worker(self) -> bool:
        if self.owned is None:
            return True
        return await asyncio.to_thread(
            stop_owned, self.owned, grace_s=self.executor.config.cancel_grace_s
        )

    async def release_lock_if_safe(self) -> None:
        if not self.lock_acquired:
            return
        if self.owned is not None and is_owned_alive(self.owned):
            return
        self.executor.workspace.lock.release()
        self.lock_acquired = False

    async def finalize(
        self,
        *,
        kind: str,
        reason: str,
        updater: TaskUpdater | None,
    ) -> str:
        async with self._cleanup:
            if self._finalized:
                return self._final_kind or kind
            self._closing = True
            stopped = await self.stop_worker()
            if not stopped:
                self._mark_recovery(
                    "could not confirm owned worker stopped; inspect owner.json and state.sqlite"
                )
                if updater is not None:
                    await updater.requires_input(
                        updater.new_agent_message(
                            [
                                new_data_part(
                                    {
                                        "reason": self.executor.state.get_execution(
                                            self.request.execution_id
                                        ).recovery_reason
                                        if self.executor.state.get_execution(self.request.execution_id)
                                        else reason,
                                        "recovery_required": True,
                                    }
                                )
                            ]
                        )
                    )
                self._finalized = True
                self._final_kind = "recovery"
                return "recovery"
            await self._capture_partial(reason=reason, canceled=(kind == "canceled"))
            lifecycle = {
                "worker_started": self.worker_launched,
                "worker_stopped": True,
                "lock_held": False,
                "evidence_dir": str(self.evidence),
                "reason": reason,
            }
            if kind == "canceled":
                self.executor.state.update_execution(
                    self.request.execution_id,
                    status="canceled",
                    recovery_required=False,
                    recovery_reason=reason,
                )
                persist_task_outcome(
                    self.executor.state,
                    task_id=self.task_id,
                    context_id=self.context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    payload={"reason": reason},
                )
                if updater is not None:
                    await updater.add_artifact(
                        [new_data_part(lifecycle)],
                        name=WORKER_LIFECYCLE_ARTIFACT,
                        last_chunk=True,
                    )
                    await updater.cancel(
                        updater.new_agent_message([new_data_part({"reason": reason})])
                    )
            else:
                self.executor.state.update_execution(
                    self.request.execution_id,
                    status="failed",
                    recovery_required=False,
                    recovery_reason=reason,
                )
                persist_task_outcome(
                    self.executor.state,
                    task_id=self.task_id,
                    context_id=self.context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    payload={"reason": reason},
                )
                if updater is not None:
                    await updater.add_artifact(
                        [new_data_part(lifecycle)],
                        name=WORKER_LIFECYCLE_ARTIFACT,
                        last_chunk=True,
                    )
                    await updater.failed(
                        updater.new_agent_message([new_data_part({"reason": reason})])
                    )
            await self.release_lock_if_safe()
            if not self.lock_acquired:
                release_lock_if_owner(self.executor.workspace.path, self.request.execution_id)
            self._finalized = True
            self._final_kind = kind
            return kind

    def _mark_recovery(self, reason: str) -> None:
        self.executor.state.update_execution(
            self.request.execution_id,
            status="recovery_required",
            recovery_required=True,
            recovery_reason=reason,
        )
        persist_task_outcome(
            self.executor.state,
            task_id=self.task_id,
            context_id=self.context_id,
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            payload={"reason": reason, "recovery_required": True},
        )

    async def _capture_partial(self, *, reason: str, canceled: bool) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        after_git = self.executor.workspace.snapshot()
        notes = ""
        try:
            notes = self.executor.workspace.read_handoff_text()
        except OSError:
            notes = ""
        payload = {
            "schema": CODING_TASK_PROFILE,
            "workflow_id": self.request.workflow_id,
            "run_id": self.request.run_id,
            "execution_id": self.request.execution_id,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "reason": reason,
            "canceled": canceled,
            "worker_launched": self.worker_launched,
            "worker_stopped": True,
            "lock_held": self.lock_acquired,
            "git_status": after_git.status,
            "diff": after_git.diff,
            "head_after": after_git.head,
            "evidence_dir": str(self.evidence),
            "execution_notes": notes,
        }
        _write_json(self.evidence / "partial-result.json", payload)
        self.executor.state.update_execution(
            self.request.execution_id, result_json=payload
        )
        # Best-effort lifecycle snapshot for canceled/failed/recovered runs.


class CodingAgentExecutor(AgentExecutor):
    def __init__(self, config: ServerConfig, state: SqliteState):
        self.config = config
        self.state = state
        self.workspace = GitWorkspace(config.workspace_id, config.workspace_path)
        self.adapter = ClaudeAdapter(config.claude)
        self.config.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._runtimes: dict[str, ExecutionRuntime] = {}

    def reconcile_startup(self) -> None:
        for claim in self.state.list_nonterminal():
            try:
                self._reconcile_claim(claim)
            except Exception:  # noqa: BLE001 — never fail server start on one row
                LOGGER.exception("startup reconcile failed for %s", claim.execution_id)
                self._persist_recovery(
                    claim,
                    "startup reconcile failed; inspect owner.json and state.sqlite",
                )
        self._heal_terminal_tasks()

    def _heal_terminal_tasks(self) -> None:
        """If the registry is terminal but the Task snapshot is not, persist the matching outcome."""
        for claim in self.state.list_all():
            mapped = EXECUTION_TO_TASK_STATE.get(claim.status)
            if mapped is None:
                continue
            task = self.state.load_task(claim.task_id)
            if task is None or task.status.state in TERMINAL_TASK_PROTO:
                continue
            reason = claim.recovery_reason
            if not reason and claim.result_json:
                try:
                    parsed = json.loads(claim.result_json)
                    if isinstance(parsed, dict) and parsed.get("reason"):
                        reason = str(parsed["reason"])
                except json.JSONDecodeError:
                    reason = None
            persist_task_outcome(
                self.state,
                task_id=claim.task_id,
                context_id=claim.context_id,
                state=mapped,
                payload={"reason": reason or f"reconciled {claim.status} execution"},
            )

    def _reconcile_claim(self, claim: ExecutionClaim) -> None:
        if claim.pid and claim.pgid and claim.start_identity:
            owned = owned_from_record(
                pid=int(claim.pid),
                pgid=int(claim.pgid),
                start_identity=claim.start_identity,
                cwd=self.workspace.path,
            )
            if pid_exists(owned.pid) and not identity_matches(owned):
                self._persist_recovery(
                    claim,
                    "uncertain process identity after restart; do not signal the live PID or remove execute.lock until the worker is confirmed stopped",
                )
                return
            if is_owned_alive(owned):
                stopped = stop_owned(owned, grace_s=self.config.cancel_grace_s)
                if not stopped:
                    self._persist_recovery(
                        claim,
                        "owned worker still running after restart stop attempt; inspect owner.json",
                    )
                    return
            self._persist_failed(claim, "interrupted by server restart")
            release_lock_if_owner(self.workspace.path, claim.execution_id)
            return
        if claim.status == "claimed" and claim.pid is None:
            self._persist_failed(claim, "interrupted before worker start (server restart)")
            release_lock_if_owner(self.workspace.path, claim.execution_id)
            return
        self._persist_recovery(
            claim,
            "uncertain process identity after restart; do not remove execute.lock until the worker is confirmed stopped",
        )

    def _persist_failed(self, claim: ExecutionClaim, reason: str) -> None:
        persist_task_outcome(
            self.state,
            task_id=claim.task_id,
            context_id=claim.context_id,
            state=TaskState.TASK_STATE_FAILED,
            payload={"reason": reason},
        )
        self.state.update_execution(
            claim.execution_id,
            status="failed",
            recovery_required=False,
            recovery_reason=reason,
            result_json={"reason": reason},
        )
        evidence = self.config.evidence_dir / claim.execution_id
        evidence.mkdir(parents=True, exist_ok=True)
        _write_json(evidence / "partial-result.json", {"reason": reason, "recovery": False})

    def _persist_recovery(self, claim: ExecutionClaim, reason: str) -> None:
        persist_task_outcome(
            self.state,
            task_id=claim.task_id,
            context_id=claim.context_id,
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            payload={"reason": reason, "recovery_required": True},
        )
        self.state.update_execution(
            claim.execution_id,
            status="recovery_required",
            recovery_required=True,
            recovery_reason=reason,
        )

    async def shutdown(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.finalize(
                kind="failed",
                reason="server shutting down",
                updater=None,
            )
        self.reconcile_startup()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        runtime = self._runtimes.get(task_id)
        claim = self.state.get_by_task(task_id)
        if claim is not None and claim.status in TERMINAL_EXECUTION:
            raise TaskNotCancelableError(message="task is already terminal")
        updater = TaskUpdater(event_queue, task_id, context.context_id or "")
        if runtime is None:
            if claim is None:
                raise TaskNotCancelableError(message="no execution is attached to this task")
            owned = None
            if claim.pid and claim.pgid and claim.start_identity:
                owned = owned_from_record(
                    pid=int(claim.pid),
                    pgid=int(claim.pgid),
                    start_identity=claim.start_identity,
                    cwd=self.workspace.path,
                )
            if owned is None or not is_owned_alive(owned):
                self._persist_failed(claim, "canceled after worker already stopped")
                await updater.cancel(
                    updater.new_agent_message([new_data_part({"reason": "canceled; worker already stopped"})])
                )
                release_lock_if_owner(self.workspace.path, claim.execution_id)
                return
            stopped = await asyncio.to_thread(
                stop_owned, owned, grace_s=self.config.cancel_grace_s
            )
            if not stopped:
                self._persist_recovery(claim, "cancel could not confirm owned worker stopped")
                await updater.requires_input(
                    updater.new_agent_message(
                        [new_data_part({"reason": claim.recovery_reason or "recovery_required", "recovery_required": True})]
                    )
                )
                return
            self.state.update_execution(claim.execution_id, status="canceled", recovery_required=False)
            await updater.cancel(
                updater.new_agent_message([new_data_part({"reason": "canceled by caller"})])
            )
            release_lock_if_owner(self.workspace.path, claim.execution_id)
            return
        await runtime.finalize(
            kind="canceled",
            reason="canceled by caller",
            updater=updater,
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ContractError("missing message")
        if context.task_id and not context.message.task_id:
            context.message.task_id = context.task_id
        if context.context_id and not context.message.context_id:
            context.message.context_id = context.context_id
        task = new_task_from_user_message(context.message)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await event_queue.enqueue_event(task)
        try:
            parts = get_data_parts(context.message.parts)
            if not parts:
                raise ContractError("SendMessage must carry a coding-task data part")
            request = parse_coding_request(parts[0])
        except ContractError as exc:
            await updater.reject(
                updater.new_agent_message([new_data_part({"reason": str(exc)})])
            )
            return
        runtime = ExecutionRuntime(self, request, task.id, task.context_id)
        self._runtimes[task.id] = runtime
        runtime.evidence.mkdir(parents=True, exist_ok=True)
        request_path = runtime.evidence / "request.json"
        if not request_path.exists():
            _write_json(
                request_path,
                request.to_dict() | {"task_id": task.id, "context_id": task.context_id},
            )
            (runtime.evidence / "handoff.md").write_bytes(request.utf8_bytes())
        try:
            await self._run_validated(runtime, updater)
        except asyncio.CancelledError:
            await runtime.finalize(
                kind="canceled",
                reason="canceled by caller",
                updater=updater,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — convert unexpected failures to FAILED
            LOGGER.exception("execution failed")
            if not runtime._finalized:
                await updater.failed(
                    updater.new_agent_message([new_data_part({"reason": str(exc)})])
                )
                self.state.update_execution(request.execution_id, status="failed")
                await runtime.release_lock_if_safe()
        finally:
            if runtime._closing and not runtime._finalized:
                async with runtime._cleanup:
                    pass
            elif runtime.owned is not None and is_owned_alive(runtime.owned) and not runtime._finalized:
                await runtime.finalize(
                    kind="failed",
                    reason="execution ended while worker still running",
                    updater=updater,
                )
            elif runtime.lock_acquired and not runtime._closing and not runtime._finalized:
                await runtime.release_lock_if_safe()
            self._runtimes.pop(task.id, None)

    async def _run_validated(self, runtime: ExecutionRuntime, updater: TaskUpdater) -> None:
        request = runtime.request
        try:
            self.workspace.lock.acquire()
            runtime.lock_acquired = True
            runtime.before_doc = self.workspace.validate_request(
                workspace_id=request.workspace_id,
                expected_branch=request.expected_branch,
                expected_head=request.expected_head,
                handoff_markdown=request.handoff_markdown,
                request_sha256=request.request_sha256,
                expected_code_fingerprint=request.expected_code_fingerprint,
            )
            runtime.before_git = self.workspace.snapshot()
        except WorkspaceBusy as exc:
            self.state.update_execution(request.execution_id, status="rejected")
            await updater.reject(
                updater.new_agent_message([new_data_part({"reason": exc.reason})])
            )
            runtime._finalized = True
            runtime._final_kind = "rejected"
            return
        except WorkspaceError as exc:
            if runtime.lock_acquired:
                self.workspace.lock.release()
                runtime.lock_acquired = False
            self.state.update_execution(request.execution_id, status="rejected")
            await updater.reject(
                updater.new_agent_message([new_data_part({"reason": exc.reason})])
            )
            runtime._finalized = True
            runtime._final_kind = "rejected"
            return

        await updater.start_work()
        stdout_path = runtime.evidence / "stdout.json"
        stderr_path = runtime.evidence / "stderr.txt"
        argv = self.adapter.argv()
        env = child_environment()
        try:
            runtime.owned = await asyncio.to_thread(
                start_owned,
                argv,
                cwd=self.workspace.path,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            runtime.worker_launched = True
            await updater.add_artifact(
                [
                    new_data_part(
                        {
                            "worker_started": True,
                            "worker_stopped": False,
                            "lock_held": True,
                            "evidence_dir": str(runtime.evidence),
                        }
                    )
                ],
                name=WORKER_LIFECYCLE_ARTIFACT,
                last_chunk=False,
            )
            self.workspace.lock.write_owner(
                {
                    "execution_id": request.execution_id,
                    "task_id": runtime.task_id,
                    "pid": runtime.owned.pid,
                    "pgid": runtime.owned.pgid,
                    "start_identity": runtime.owned.start_identity,
                }
            )
            self.state.update_execution(
                request.execution_id,
                status="running",
                pid=runtime.owned.pid,
                pgid=runtime.owned.pgid,
                start_identity=runtime.owned.start_identity,
                evidence_dir=str(runtime.evidence),
            )
            exit_code = await asyncio.to_thread(
                wait_owned, runtime.owned, timeout_s=self.config.execution_timeout_s
            )
            if exit_code is None:
                await runtime.finalize(
                    kind="failed",
                    reason=f"execution deadline exceeded ({self.config.execution_timeout_s}s)",
                    updater=updater,
                )
                return
            if runtime._finalized or runtime._closing:
                return
            duration_s = time.monotonic() - runtime.started_at
            outcome = interpret_claude_files(
                stdout_path,
                stderr_path,
                exit_code=exit_code,
                duration_s=duration_s,
                argv=tuple(argv),
            )
            after_git = self.workspace.snapshot()
            after_doc, transition_ok, transition_reason = self.workspace.evaluate_transition(
                runtime.before_doc
            )
            commits = tuple(
                CommitRef(sha=sha, subject=subject)
                for sha, subject in self.workspace.commits_between(
                    runtime.before_git.head, after_git.head
                )
            )
            invalid_output = outcome.invalid_output
            provider_error = outcome.provider_error
            failed = (
                outcome.exit_code != 0
                or provider_error
                or invalid_output
                or not transition_ok
            )
            if outcome.exit_code != 0:
                reason = f"subprocess exited {outcome.exit_code}"
            elif provider_error:
                reason = "provider reported is_error=true"
            elif invalid_output:
                reason = "provider output was missing or not valid JSON"
            elif not transition_ok:
                reason = transition_reason
            else:
                reason = None
            result = CodingResult(
                schema=CODING_TASK_PROFILE,
                workflow_id=request.workflow_id,
                run_id=request.run_id,
                execution_id=request.execution_id,
                task_id=runtime.task_id,
                context_id=runtime.context_id,
                iteration=request.iteration,
                workspace_id=request.workspace_id,
                summary=outcome.summary,
                execution_notes=after_doc.execution_notes,
                exit_code=outcome.exit_code,
                provider_error=provider_error,
                invalid_output=invalid_output,
                invalid_handoff_transition=not transition_ok,
                reason=reason,
                status_before=runtime.before_doc.status,
                status_after=after_doc.status,
                branch_before=runtime.before_git.branch,
                branch_after=after_git.branch,
                head_before=runtime.before_git.head,
                head_after=after_git.head,
                commits=commits,
                git_status=after_git.status,
                diff=after_git.diff,
                duration_s=round(outcome.duration_s, 3),
                usage=outcome.usage,
                cost_usd=outcome.cost_usd,
                usage_provenance=outcome.usage_provenance,
                cost_provenance=outcome.cost_provenance,
                evidence_dir=str(runtime.evidence),
                plan_intact=after_doc.planner_fingerprint == runtime.before_doc.planner_fingerprint,
                worker_launched=runtime.worker_launched,
            )
            payload = result.to_dict()
            payload["worker_stopped"] = True
            payload["lock_held"] = False
            _write_json(runtime.evidence / "result.json", payload)
            _write_json(
                runtime.evidence / "metadata.json",
                {
                    "workflow_id": request.workflow_id,
                    "run_id": request.run_id,
                    "execution_id": request.execution_id,
                    "task_id": runtime.task_id,
                    "context_id": runtime.context_id,
                    "iteration": request.iteration,
                },
            )
            self.state.update_execution(
                request.execution_id,
                status="failed" if failed else "completed",
                result_json=payload,
                recovery_required=False,
            )
            await updater.add_artifact(
                [new_data_part(payload)],
                name=CODING_RESULT_ARTIFACT,
                last_chunk=True,
            )
            await updater.add_artifact(
                [
                    new_data_part(
                        {
                            "worker_started": runtime.worker_launched,
                            "worker_stopped": True,
                            "lock_held": False,
                            "evidence_dir": str(runtime.evidence),
                        }
                    )
                ],
                name=WORKER_LIFECYCLE_ARTIFACT,
                last_chunk=True,
            )
            if failed:
                await updater.failed(
                    updater.new_agent_message([new_data_part({"reason": reason})])
                )
            else:
                await updater.complete()
            runtime._finalized = True
            runtime._final_kind = "failed" if failed else "completed"
        finally:
            if runtime.lock_acquired and (runtime.owned is None or not is_owned_alive(runtime.owned)):
                await runtime.release_lock_if_safe()


class ClaimAwareHandler:
    """Select task/context IDs at the claim boundary before the SDK creates a task."""

    def __init__(self, inner: DefaultRequestHandlerV2, state: SqliteState):
        self._inner = inner
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def on_message_send(self, params: SendMessageRequest, context: Any) -> Task | Any:
        try:
            request = _extract_coding_request(params)
        except ContractError:
            return await self._inner.on_message_send(params, context)
        request_hash = request_canonical_hash(request)
        try:
            claim = self._state.claim(
                execution_id=request.execution_id,
                request_hash=request_hash,
                request=request.to_dict(),
                run_id=request.run_id,
                workflow_id=request.workflow_id,
                task_id=params.message.task_id or None,
                context_id=params.message.context_id or None,
            )
        except ClaimConflict as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        params.message.task_id = claim.task_id
        params.message.context_id = claim.context_id
        if claim.status in TERMINAL_EXECUTION or claim.status == "recovery_required":
            stored = self._state.load_task(claim.task_id)
            if stored is not None:
                return stored
        if not self._state.try_dispatch(request.execution_id):
            stored = self._state.load_task(claim.task_id)
            if stored is not None:
                return stored
        return await self._inner.on_message_send(params, context)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _has_valid_bearer(request: Request, credential: str) -> bool:
    header = request.headers.get("authorization", "")
    expected = f"Bearer {credential}"
    if len(header) != len(expected):
        return False
    return hmac.compare_digest(header, expected)


def create_app(config: ServerConfig) -> Starlette:
    card = build_agent_card(config)
    state = SqliteState(config.resolved_state_db(), config.caller_id)
    executor = CodingAgentExecutor(config, state)
    inner = DefaultRequestHandlerV2(
        agent_executor=executor,
        task_store=SqliteTaskStore(state),
        agent_card=card,
    )
    handler = ClaimAwareHandler(inner, state)
    sdk_endpoint = create_jsonrpc_routes(handler, RPC_PATH)[0].endpoint

    async def jsonrpc_endpoint(request: Request) -> Response:
        if not _has_valid_bearer(request, config.credential):
            return _unauthorized()
        return await sdk_endpoint(request)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        await asyncio.to_thread(executor.reconcile_startup)
        yield
        await executor.shutdown()
        await handler.aclose()
        state.close()

    return Starlette(
        routes=create_agent_card_routes(card)
        + [Route(RPC_PATH, jsonrpc_endpoint, methods=["POST"])],
        lifespan=lifespan,
    )


def serve(config: ServerConfig) -> None:
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
