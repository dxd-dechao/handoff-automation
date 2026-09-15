"""Development CLI: `handoff-a2a serve` and client helpers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from handoff_a2a.client import (
    ClientError,
    UnresolvedExecution,
    cancel_from_record,
    execute_exit_code,
    execute_repo,
    resume_from_record,
    status_from_record,
)
from handoff_a2a.server import load_server_config, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff-a2a",
        description=(
            "Experimental A2A development helper. Does not replace `handoff execute`. "
            "There is no arbitrary instruction/prompt argument."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser("serve", help="start a loopback-only Executor server")
    serve_cmd.add_argument("--config", required=True, type=Path, help="server.json")

    execute_cmd = sub.add_parser(
        "execute",
        help="submit the local HANDOFF.md snapshot once, poll, and print the terminal result",
    )
    execute_cmd.add_argument("--repo", required=True, type=Path)
    execute_cmd.add_argument("--agent-card-url", required=True)
    execute_cmd.add_argument("--credential-file", required=True, type=Path)
    execute_cmd.add_argument("--workspace-id", required=True)
    execute_cmd.add_argument("--timeout", type=float, default=180.0)

    status_cmd = sub.add_parser("status", help="inspect a saved run record via GetTask")
    status_cmd.add_argument("--run-record", required=True, type=Path)
    status_cmd.add_argument("--credential-file", required=True, type=Path)

    resume_cmd = sub.add_parser(
        "resume",
        help="poll a known task or retransmit the exact saved request",
    )
    resume_cmd.add_argument("--run-record", required=True, type=Path)
    resume_cmd.add_argument("--credential-file", required=True, type=Path)
    resume_cmd.add_argument("--timeout", type=float, default=180.0)

    cancel_cmd = sub.add_parser("cancel", help="CancelTask for a known saved task ID")
    cancel_cmd.add_argument("--run-record", required=True, type=Path)
    cancel_cmd.add_argument("--credential-file", required=True, type=Path)
    return parser


def _print_unresolved(exc: UnresolvedExecution) -> int:
    print(
        json.dumps(
            {
                "unresolved": True,
                "task_id": exc.task_id,
                "execution_id": exc.execution_id,
                "error": str(exc),
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        config = load_server_config(args.config)
        serve(config)
        return 0
    if args.command == "execute":
        try:
            payload = asyncio.run(
                execute_repo(
                    repo=args.repo,
                    agent_card_url=args.agent_card_url,
                    credential_file=args.credential_file,
                    workspace_id=args.workspace_id,
                    timeout=args.timeout,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        result = payload.get("result") or {}
        state = (payload.get("task") or {}).get("status", {}).get("state")
        print(
            json.dumps(
                {
                    "task_id": payload.get("task_id"),
                    "execution_id": payload.get("execution_id"),
                    "state": state,
                    "reason": payload.get("reason") or result.get("reason"),
                    "evidence_dir": result.get("evidence_dir"),
                    "local_metadata": payload.get("local_metadata"),
                    "run_record": payload.get("run_record"),
                    "result": result,
                },
                indent=2,
            )
        )
        return execute_exit_code(state)
    if args.command == "status":
        try:
            payload = asyncio.run(
                status_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2, default=str))
        if payload.get("unresolved"):
            return 2
        return 0
    if args.command == "resume":
        try:
            payload = asyncio.run(
                resume_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                    timeout=args.timeout,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        state = (payload.get("task") or {}).get("status", {}).get("state")
        print(
            json.dumps(
                {
                    "task_id": payload.get("task_id"),
                    "execution_id": payload.get("execution_id"),
                    "state": state,
                    "reason": payload.get("reason"),
                    "run_record": payload.get("run_record"),
                    "recovery_required": payload.get("recovery_required"),
                    "result": payload.get("result"),
                },
                indent=2,
                default=str,
            )
        )
        return execute_exit_code(state)
    if args.command == "cancel":
        try:
            payload = asyncio.run(
                cancel_from_record(
                    record_path=args.run_record,
                    credential_file=args.credential_file,
                )
            )
        except UnresolvedExecution as exc:
            return _print_unresolved(exc)
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2, default=str))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
