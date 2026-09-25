"""Managed local Executor service: `handoff server start|status|stop`.

The generated server runs the existing `handoff-a2a serve` implementation as
a child in its own session, so it outlives the short CLI command; there is no
OS-wide daemon or LaunchAgent. Ownership is a process record (PID, process
group, start identity) plus verification through the service itself: the
Agent Card must name this workspace, the configured provider/model, and the
config generation, and an authenticated JSON-RPC probe must succeed with the
generated token. A matching TCP port alone is never enough, and nothing
without a verified record is ever signaled.

Lock order (shared with selection.py and integration.py):
  service.lock (start/stop/switch/setup)  ->  submit.lock (A2A submission)
Stop and switch take both; start takes service.lock; a submission takes only
submit.lock. None of them waits for a worker while holding a lock that
reconciliation needs: unresolved work is refused, not waited for.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from handoff_a2a.client import write_json_atomic
from handoff_a2a.config import ConfigError, HandoffConfig, load_config
from handoff_a2a.contracts import CODING_TASK_PROFILE
from handoff_a2a.processes import (
    identity_matches,
    owned_from_record,
    process_start_identity,
    start_owned,
    stop_owned,
)
from handoff_a2a.reporting import print_json, print_json_error
from handoff_a2a.setup import (
    ManagedPaths,
    SetupError,
    card_url,
    holding,
    read_json,
    selection_of,
    service_lock,
)
from handoff_a2a.workspace import submit_lock

PROCESS_NAME = "process.json"
WATCHER_NAME = "watcher.json"
FAILURE_NAME = "last-failure.json"
START_TIMEOUT_S = 30.0
STOP_GRACE_S = 10.0
NONTERMINAL = ("claimed", "running", "recovery_required")


class ServiceError(SetupError):
    pass


@dataclass
class ServiceState:
    running: bool  # a live process owned by our record
    verified: bool  # card identity + authenticated probe match the configuration
    port: int
    generation: int | None
    provider: str
    model: str
    card: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    detail: list[str] = field(default_factory=list)

    @property
    def active_identity(self) -> str:
        if not self.card:
            return "none"
        return f"{self.card.get('executor_provider')} / {self.card.get('executor_model')} (generation {self.card.get('config_generation')})"


@dataclass
class Managed:
    paths: ManagedPaths
    config: HandoffConfig
    server: dict[str, Any]

    @property
    def port(self) -> int:
        return int(self.server["port"])

    @property
    def generation(self) -> int:
        return int(self.server.get("config_generation") or 1)

    @property
    def workspace_id(self) -> str:
        return str(self.server["workspace_id"])


def unmanaged_hint(repo: Path) -> str:
    q = f'"{repo}"'
    return (
        "this repository has no CLI-managed A2A service "
        "(no .handoff-config.json, or it points to a manually configured endpoint). "
        "Managed server/model commands never change an unrelated service. "
        f'To migrate explicitly: handoff init {q} --transport a2a --executor <provider> --model "<model-id>" '
        "(refuses while work is unresolved and keeps a copy of the old settings)"
    )


def load_managed(repo: Path) -> Managed:
    repo = repo.resolve()
    try:
        config = load_config(repo)
    except ConfigError as exc:
        raise ServiceError(f"invalid .handoff-config.json: {exc}") from exc
    if config is None or config.managed is None or config.transport != "a2a":
        raise ServiceError(unmanaged_hint(repo), exit_code=2)
    paths = ManagedPaths(repo)
    if config.managed.server_config != paths.server_config.resolve():
        raise ServiceError(f"managed server config must be {paths.server_config}; found {config.managed.server_config}")
    if not paths.server_config.is_file():
        raise ServiceError(f"missing {paths.server_config}; re-run handoff init")
    server = read_json(paths.server_config)
    if str(server.get("workspace_path")) != str(repo):
        raise ServiceError(f"{paths.server_config} names another workspace ({server.get('workspace_path')})")
    return Managed(paths, config, server)


# ── process record ──────────────────────────────────────────────────────────


def process_path(paths: ManagedPaths) -> Path:
    return paths.service_dir / PROCESS_NAME


def load_process(paths: ManagedPaths) -> dict[str, Any] | None:
    path = process_path(paths)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def owned(record: dict[str, Any], paths: ManagedPaths):
    return owned_from_record(
        pid=int(record.get("pid") or 0),
        pgid=int(record.get("pgid") or 0),
        start_identity=str(record.get("start_identity") or ""),
        cwd=paths.service_dir,
    )


def record_alive(record: dict[str, Any] | None, paths: ManagedPaths) -> bool:
    """True only for the exact process we started (PID + start identity)."""
    if not record or not record.get("pid"):
        return False
    return identity_matches(owned(record, paths))


# ── watcher record ──────────────────────────────────────────────────────────
#
# `handoff watch` (A2A) records itself so status and the Planner skill can tell
# whether a watcher is running. Same rule as the service record: a bare PID is
# never trusted; the start identity must match the live process.


def watcher_path(repo: Path) -> Path:
    return ManagedPaths(repo).service_dir / WATCHER_NAME


def _load_watcher(repo: Path) -> dict[str, Any] | None:
    path = watcher_path(repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _watcher_alive(record: dict[str, Any], repo: Path) -> bool:
    if not record.get("pid"):
        return False
    return identity_matches(
        owned_from_record(pid=int(record["pid"]), pgid=0, start_identity=str(record.get("start_identity") or ""), cwd=repo)
    )


def watcher_state(repo: Path) -> dict[str, Any]:
    record = _load_watcher(repo)
    if record is None:
        return {"running": False, "stale": False, "pid": None, "started_at": None}
    alive = _watcher_alive(record, repo)
    return {"running": alive, "stale": not alive, "pid": record.get("pid"), "started_at": record.get("started_at")}


def claim_watcher(repo: Path, interval: float) -> dict[str, Any]:
    """Record this process as the repository's watcher; refuse a second live one."""
    existing = _load_watcher(repo)
    if existing and _watcher_alive(existing, repo) and int(existing["pid"]) != os.getpid():
        raise ServiceError(
            f"a handoff watch is already running for this repository (pid {existing['pid']} since "
            f"{existing.get('started_at')}); stop it first or keep using it",
            exit_code=2,
        )
    record = {
        "pid": os.getpid(),
        "start_identity": process_start_identity(os.getpid()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interval_s": interval,
    }
    watcher_path(repo).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(watcher_path(repo), record)
    return record


def release_watcher(repo: Path, record: dict[str, Any]) -> None:
    current = _load_watcher(repo)
    if current and current.get("pid") == record["pid"] and current.get("start_identity") == record["start_identity"]:
        watcher_path(repo).unlink(missing_ok=True)


# ── probes ──────────────────────────────────────────────────────────────────


def port_listening(port: int, timeout: float = 0.5) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    return False


def probe_card(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Coding-profile identity params from the Agent Card, or None."""
    try:
        response = httpx.get(card_url(port), timeout=timeout, trust_env=False)
        response.raise_for_status()
        card = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    for ext in ((card.get("capabilities") or {}).get("extensions") or []):
        if ext.get("uri") == CODING_TASK_PROFILE:
            params = dict(ext.get("params") or {})
            generation = params.get("config_generation")
            if isinstance(generation, float) and generation.is_integer():
                params["config_generation"] = int(generation)
            params["name"] = card.get("name")
            return params
    return None


def authenticated_probe(port: int, token: str, timeout: float = 2.0) -> bool:
    """A GetTask for a random ID must reach JSON-RPC (not 401) with the local token."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": str(uuid.uuid4())}}
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/a2a",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0", "A2A-Extensions": CODING_TASK_PROFILE},
            timeout=timeout,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    if response.status_code == 401:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("jsonrpc") == "2.0"


def expected_identity(managed: Managed) -> dict[str, Any]:
    provider, model, _effort = selection_of(managed.server)
    return {
        "workspace_id": managed.workspace_id,
        "config_generation": managed.generation,
        "executor_provider": provider,
        "executor_model": model,
    }


def identity_mismatches(card: dict[str, Any] | None, expected: dict[str, Any]) -> list[str]:
    if card is None:
        return ["no coding-task Agent Card"]
    return [
        f"{key}: service has {card.get(key)!r}, configuration has {value!r}"
        for key, value in expected.items()
        if card.get(key) != value
    ]


def inspect(managed: Managed) -> ServiceState:
    paths = managed.paths
    record = load_process(paths)
    alive = record_alive(record, paths)
    expected = expected_identity(managed)
    card = probe_card(managed.port) if port_listening(managed.port) else None
    state = ServiceState(
        running=alive,
        verified=False,
        port=managed.port,
        generation=managed.generation,
        provider=expected["executor_provider"],
        model=expected["executor_model"],
        card=card,
        record=record,
    )
    if record and not alive:
        state.detail.append("process record is stale (that process is gone or its PID was reused)")
    if alive and record and int(record.get("port") or 0) != managed.port:
        state.detail.append(f"owned server listens on {record.get('port')}, configuration names {managed.port}")
    if card is not None and not alive:
        state.detail.append(f"port {managed.port} answers but not from a process this CLI started")
    mismatches = identity_mismatches(card, expected) if alive else []
    if alive and not mismatches:
        token = paths.token.read_text(encoding="utf-8").strip() if paths.token.is_file() else ""
        if token and authenticated_probe(managed.port, token):
            state.verified = True
        else:
            state.detail.append("authenticated probe failed (token mismatch or service not answering)")
    state.detail.extend(mismatches)
    return state


# ── quiescence ──────────────────────────────────────────────────────────────


def nonterminal_claims(state_db: Path) -> list[tuple[str, str]]:
    if not state_db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return [("?", f"state database unreadable: {exc}")]
    try:
        rows = conn.execute(
            "SELECT execution_id, status FROM executions WHERE status IN (?, ?, ?)", NONTERMINAL
        ).fetchall()
    except sqlite3.Error as exc:
        return [("?", f"state database unreadable: {exc}")]
    finally:
        conn.close()
    return [(str(a), str(b)) for a, b in rows]


def unresolved(managed: Managed, *, holding_submit: bool = False) -> str | None:
    logs = managed.paths.logs
    if (logs / "outstanding.json").is_file():
        return "an A2A run is outstanding (submitted, working, or acknowledgement unknown)"
    if not holding_submit and (logs / "submit.lock").exists():
        return "a submission is in progress (submit.lock)"
    if (logs / "execute.lock").exists():
        return "execute.lock is held (a worker may be running or awaiting recovery)"
    claims = nonterminal_claims(Path(str(managed.server.get("state_db") or managed.paths.state_db)))
    if claims:
        execution_id, status = claims[0]
        return f"the server has unresolved execution {execution_id} ({status})"
    return None


def refusal_guidance(repo: Path) -> str:
    q = f'"{repo}"'
    return f"see handoff status {q}; then handoff resume {q} or handoff cancel {q}"


# ── start / stop ────────────────────────────────────────────────────────────


def _failure(paths: ManagedPaths, payload: dict[str, Any]) -> None:
    write_json_atomic(paths.service_dir / FAILURE_NAME, {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload})


def _log_tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start_locked(managed: Managed, *, timeout_s: float = START_TIMEOUT_S) -> ServiceState:
    """Start (or confirm) the managed server. Caller holds service.lock."""
    paths = managed.paths
    state = inspect(managed)
    if state.verified:
        state.detail.insert(0, "already running")
        return state
    if state.running:
        raise ServiceError(
            "the owned server is running but does not match the configuration "
            f"({'; '.join(state.detail)}); run handoff server stop first"
        )
    if port_listening(managed.port):
        raise ServiceError(
            f"port {managed.port} is in use by a process this CLI did not start; nothing was stopped. "
            f'Choose another port: handoff server start "{paths.repo}" --port <free-port>'
        )
    if state.record is not None:
        process_path(paths).unlink(missing_ok=True)  # stale: never signaled
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = paths.service_dir / f"server-{stamp}.log"
    argv = [sys.executable, "-m", "handoff_a2a", "serve", "--config", str(paths.server_config)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("HANDOFF_A2A_HOLD")}
    child = start_owned(argv, cwd=paths.service_dir, env=env, stdout_path=log, stderr_path=log.with_suffix(".err.log"))
    record = {
        "pid": child.pid,
        "pgid": child.pgid,
        "start_identity": child.start_identity,
        "port": managed.port,
        "workspace_id": managed.workspace_id,
        "config_generation": managed.generation,
        "provider": state.provider,
        "model": state.model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "log": str(log.with_suffix(".err.log")),
        "argv": argv,
    }
    write_json_atomic(process_path(paths), record)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        child.process.poll()
        if child.process.returncode is not None:
            break
        state = inspect(managed)
        if state.verified:
            return state
        time.sleep(0.2)
    # Not verified in time: stop exactly the child we started, keep diagnosis.
    stopped = stop_owned(child, grace_s=3.0)
    if stopped:
        process_path(paths).unlink(missing_ok=True)
    tail = _log_tail(log.with_suffix(".err.log"))
    _failure(paths, {"event": "start_failed", "generation": managed.generation, "provider": state.provider, "model": state.model, "stopped": stopped, "log": record["log"]})
    raise ServiceError(
        f"server did not become ready with the expected identity within {timeout_s:.0f}s "
        f"({'; '.join(state.detail) or 'no answer'}); log: {record['log']}" + (f"\n{tail}" if tail else "")
    )


def stop_locked(managed: Managed, *, holding_submit: bool) -> str:
    """Stop the owned server if idle. Caller holds service.lock (and submit.lock)."""
    paths = managed.paths
    reason = unresolved(managed, holding_submit=holding_submit)
    if reason:
        raise ServiceError(f"refusing to stop the service: {reason}; {refusal_guidance(paths.repo)}", exit_code=2)
    record = load_process(paths)
    if not record_alive(record, paths):
        if record is not None:
            process_path(paths).unlink(missing_ok=True)
            return "not running (removed a stale process record; nothing was signaled)"
        if port_listening(managed.port):
            return f"not running under this CLI; port {managed.port} is used by another process (left alone)"
        return "not running"
    assert record is not None
    if not stop_owned(owned(record, paths), grace_s=STOP_GRACE_S):
        raise ServiceError(f"could not confirm the server (pid {record.get('pid')}) stopped; inspect it before retrying")
    process_path(paths).unlink(missing_ok=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and port_listening(managed.port):
        time.sleep(0.1)
    return "stopped"


def set_port(managed: Managed, port: int) -> Managed:
    """Move an idle, stopped managed service to another loopback port."""
    if not 1024 <= port <= 65535:
        raise ServiceError("--port must be 1024-65535")
    reason = unresolved(managed)
    if reason:
        raise ServiceError(f"refusing to change the port: {reason}", exit_code=2)
    server = dict(managed.server)
    server["port"] = port
    server.pop("public_base_url", None)
    config = read_json(managed.paths.config)
    config["a2a"]["agent_card_url"] = card_url(port)
    write_json_atomic(managed.paths.server_config, server)
    write_json_atomic(managed.paths.config, config)
    return load_managed(managed.paths.repo)


def cmd_start(repo: Path, port: int | None, as_json: bool = False) -> int:
    managed = load_managed(repo)
    with holding(service_lock(managed.paths), "service"):
        if port is not None and port != managed.port:
            if record_alive(load_process(managed.paths), managed.paths):
                raise ServiceError("stop the running service before changing its port")
            managed = set_port(managed, port)
        state = start_locked(managed)
    if as_json:
        print_json("server-status", state_payload(managed, state))
    else:
        print_state(managed, state)
    return 0


def cmd_stop(repo: Path, as_json: bool = False) -> int:
    managed = load_managed(repo)
    with holding(service_lock(managed.paths), "service"):
        with holding(submit_lock(managed.paths.repo), "submission"):
            outcome = stop_locked(managed, holding_submit=True)
    if as_json:
        print_json("server-stop", {"repo": str(managed.paths.repo), "outcome": outcome})
    else:
        print(f"service: {outcome}")
    return 0


def cmd_status(repo: Path, as_json: bool = False) -> int:
    managed = load_managed(repo)
    state = inspect(managed)
    if as_json:
        print_json("server-status", state_payload(managed, state))
    else:
        print_state(managed, state)
    return 0 if state.verified or not state.running else 2


def state_payload(managed: Managed, state: ServiceState) -> dict[str, Any]:
    """`handoff server status --json` (no token or credential contents)."""
    running = state.running
    failure = None
    path = managed.paths.service_dir / FAILURE_NAME
    if path.is_file() and not state.verified:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(path.read_text(encoding="utf-8"))
            failure = {k: data.get(k) for k in ("event", "at", "provider", "model", "log")}
    return {
        "repo": str(managed.paths.repo),
        "service": "running" if running else "stopped",
        "verified": state.verified,
        "endpoint": card_url(managed.port),
        "port": managed.port,
        "selected": {"provider": state.provider, "model": state.model, "generation": managed.generation},
        "active": None
        if not (running and state.card)
        else {
            "provider": state.card.get("executor_provider"),
            "model": state.card.get("executor_model"),
            "generation": state.card.get("config_generation"),
        },
        "pid": (state.record or {}).get("pid") if running else None,
        "started_at": (state.record or {}).get("started_at") if running else None,
        "log": (state.record or {}).get("log") if running else None,
        "notes": list(state.detail),
        "last_failure": failure,
    }


def print_state(managed: Managed, state: ServiceState) -> None:
    label = "running (verified)" if state.verified else ("running (UNVERIFIED)" if state.running else "stopped")
    print(f"service:  {label}")
    print(f"endpoint: {card_url(managed.port)}")
    print(f"selected: {state.provider} / {state.model} (generation {managed.generation})")
    print(f"active:   {state.active_identity if state.running else 'none'}")
    if state.record and state.running:
        print(f"process:  pid {state.record.get('pid')} since {state.record.get('started_at')}")
        print(f"log:      {state.record.get('log')}")
    for line in state.detail:
        print(f"note:     {line}")
    failure = managed.paths.service_dir / FAILURE_NAME
    if failure.is_file() and not state.verified:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(failure.read_text(encoding="utf-8"))
            print(f"last failure: {data.get('event')} at {data.get('at')} ({data.get('provider')} / {data.get('model')})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff server")
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--port", type=int)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.action == "start":
            return cmd_start(repo, args.port, args.json)
        if args.action == "stop":
            if args.port is not None:
                raise ServiceError("--port applies to start only")
            return cmd_stop(repo, args.json)
        return cmd_status(repo, args.json)
    except SetupError as exc:
        if args.json:
            return print_json_error(f"server-{args.action}", str(exc), exc.exit_code)
        print(f"handoff: {exc}", file=sys.stderr)
        return exc.exit_code
