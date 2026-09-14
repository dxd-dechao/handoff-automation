"""Development CLI: `handoff-a2a serve` and `handoff-a2a execute`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from handoff_a2a.client import ClientError, UnresolvedExecution, execute_repo
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
    return parser


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
        except (ClientError, OSError, ValueError) as exc:
            print(f"handoff-a2a: {exc}", file=sys.stderr)
            return 1
        result = payload.get("result") or {}
        print(
            json.dumps(
                {
                    "task_id": payload.get("task_id"),
                    "execution_id": payload.get("execution_id"),
                    "state": (payload.get("task") or {}).get("status", {}).get("state"),
                    "evidence_dir": result.get("evidence_dir"),
                    "local_metadata": payload.get("local_metadata"),
                    "result": result,
                },
                indent=2,
            )
        )
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
