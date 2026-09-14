"""Loopback A2A server: Agent Card, JSON-RPC, workspace lock, Claude adapter."""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
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
from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)
from a2a.utils.errors import TaskNotCancelableError, UnsupportedOperationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from handoff_a2a.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from handoff_a2a.contracts import (
    CODING_RESULT_ARTIFACT,
    CODING_TASK_PROFILE,
    CodingRequest,
    CodingResult,
    CommitRef,
    ContractError,
    parse_coding_request,
)
from handoff_a2a.workspace import GitWorkspace, WorkspaceBusy, WorkspaceError

LOGGER = logging.getLogger("handoff_a2a.server")
RPC_PATH = "/a2a"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def load_server_config(path: Path) -> ServerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("server config must be a JSON object")
    host = str(raw.get("host") or "")
    if not _is_loopback_host(host):
        raise ValueError(
            f"host {host!r} is not loopback-only; A1 binds 127.0.0.1, ::1, or localhost"
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
    )


def build_agent_card(config: ServerConfig) -> AgentCard:
    rpc_url = f"{config.public_base_url}{RPC_PATH}"
    return AgentCard(
        name="handoff-a2a Claude Executor",
        description=(
            "Experimental local coding Executor. Task state is in-memory and not "
            "restart-durable. Cancellation is not supported in A1. A COMPLETED "
            "task is ready for independent Planner QA, not APPROVED."
        ),
        version="0.1.0",
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


class CodingAgentExecutor(AgentExecutor):
    def __init__(self, config: ServerConfig):
        self.config = config
        self.workspace = GitWorkspace(config.workspace_id, config.workspace_path)
        self.adapter = ClaudeAdapter(config.claude)
        self.config.evidence_dir.mkdir(parents=True, exist_ok=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(
            message="CancelTask is not supported until A2; the worker was not canceled"
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ContractError("missing message")
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
        evidence = self.config.evidence_dir / request.execution_id
        evidence.mkdir(parents=True, exist_ok=True)
        _write_json(evidence / "request.json", request.to_dict() | {"task_id": task.id, "context_id": task.context_id})
        (evidence / "handoff.md").write_bytes(request.utf8_bytes())
        try:
            await self._run_validated(request, task.id, task.context_id, updater, evidence)
        except Exception as exc:  # noqa: BLE001 — convert unexpected failures to FAILED
            LOGGER.exception("execution failed")
            await updater.failed(
                updater.new_agent_message([new_data_part({"reason": str(exc)})])
            )

    async def _run_validated(
        self,
        request: CodingRequest,
        task_id: str,
        context_id: str,
        updater: TaskUpdater,
        evidence: Path,
    ) -> None:
        lock_acquired = False
        worker_launched = False
        before_git = None
        before_doc = None
        try:
            self.workspace.lock.acquire()
            lock_acquired = True
            before_doc = self.workspace.validate_request(
                workspace_id=request.workspace_id,
                expected_branch=request.expected_branch,
                expected_head=request.expected_head,
                handoff_markdown=request.handoff_markdown,
                request_sha256=request.request_sha256,
            )
            before_git = self.workspace.snapshot()
        except WorkspaceBusy as exc:
            await updater.reject(
                updater.new_agent_message([new_data_part({"reason": exc.reason})])
            )
            return
        except WorkspaceError as exc:
            if lock_acquired:
                self.workspace.lock.release()
                lock_acquired = False
            await updater.reject(
                updater.new_agent_message([new_data_part({"reason": exc.reason})])
            )
            return

        await updater.start_work()
        stdout_path = evidence / "stdout.json"
        stderr_path = evidence / "stderr.txt"
        try:
            worker_launched = True
            outcome = await self.adapter.run(self.workspace.path, stdout_path, stderr_path)
            after_git = self.workspace.snapshot()
            after_doc, transition_ok, transition_reason = self.workspace.evaluate_transition(
                before_doc
            )
            commits = tuple(
                CommitRef(sha=sha, subject=subject)
                for sha, subject in self.workspace.commits_between(
                    before_git.head, after_git.head
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
                task_id=task_id,
                context_id=context_id,
                iteration=request.iteration,
                workspace_id=request.workspace_id,
                summary=outcome.summary,
                execution_notes=after_doc.execution_notes,
                exit_code=outcome.exit_code,
                provider_error=provider_error,
                invalid_output=invalid_output,
                invalid_handoff_transition=not transition_ok,
                reason=reason,
                status_before=before_doc.status,
                status_after=after_doc.status,
                branch_before=before_git.branch,
                branch_after=after_git.branch,
                head_before=before_git.head,
                head_after=after_git.head,
                commits=commits,
                git_status=after_git.status,
                diff=after_git.diff,
                duration_s=round(outcome.duration_s, 3),
                usage=outcome.usage,
                cost_usd=outcome.cost_usd,
                usage_provenance=outcome.usage_provenance,
                cost_provenance=outcome.cost_provenance,
                evidence_dir=str(evidence),
                plan_intact=after_doc.planner_fingerprint == before_doc.planner_fingerprint,
                worker_launched=worker_launched,
            )
            payload = result.to_dict()
            _write_json(evidence / "result.json", payload)
            _write_json(
                evidence / "metadata.json",
                {
                    "workflow_id": request.workflow_id,
                    "run_id": request.run_id,
                    "execution_id": request.execution_id,
                    "task_id": task_id,
                    "context_id": context_id,
                    "iteration": request.iteration,
                },
            )
            await updater.add_artifact(
                [new_data_part(payload)],
                name=CODING_RESULT_ARTIFACT,
                last_chunk=True,
            )
            if failed:
                await updater.failed(
                    updater.new_agent_message([new_data_part({"reason": reason})])
                )
            else:
                await updater.complete()
        finally:
            if lock_acquired:
                self.workspace.lock.release()
                lock_acquired = False


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
    executor = CodingAgentExecutor(config)
    handler = DefaultRequestHandlerV2(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    sdk_endpoint = create_jsonrpc_routes(handler, RPC_PATH)[0].endpoint

    async def jsonrpc_endpoint(request: Request) -> Response:
        if not _has_valid_bearer(request, config.credential):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return await sdk_endpoint(request)
        method = body.get("method") if isinstance(body, dict) else None
        if method == "CancelTask":
            error = TaskNotCancelableError(
                message="CancelTask is not supported until A2; the worker was not canceled"
            )
            return JSONResponse(build_error_response(body.get("id"), error))
        return await sdk_endpoint(request)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        await handler.aclose()

    return Starlette(
        routes=create_agent_card_routes(card)
        + [Route(RPC_PATH, jsonrpc_endpoint, methods=["POST"])],
        lifespan=lifespan,
    )


def serve(config: ServerConfig) -> None:
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
