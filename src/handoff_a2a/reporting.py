"""Neutral run manifests and lifecycle events for integrated A2A executions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from handoff_a2a.client import write_json_atomic
from handoff_a2a.workspace import LOG_DIRNAME

MANIFEST_SCHEMA = "urn:handoff-automation:coding-task:v1#manifest"
EVENT_SCHEMA = "urn:handoff-automation:coding-task:v1#event"


def manifest_path(repo: Path, run_id: str) -> Path:
    return repo / LOG_DIRNAME / f"{run_id}-manifest.json"


def request_snapshot_path(repo: Path, run_id: str) -> Path:
    return repo / LOG_DIRNAME / f"{run_id}-request.json"


def log_path(repo: Path, run_id: str) -> Path:
    return repo / LOG_DIRNAME / f"{run_id}-execute.log"


def result_path(repo: Path, run_id: str) -> Path:
    return repo / LOG_DIRNAME / f"{run_id}-result.json"


def events_path(repo: Path, run_id: str) -> Path:
    return repo / LOG_DIRNAME / f"{run_id}-events.jsonl"


def append_event(repo: Path, run_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
    path = events_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"schema": EVENT_SCHEMA, "run_id": run_id, "kind": kind, **(payload or {})}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str) + "\n")


def usage_fields(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {
            "usage": None,
            "cost_usd": None,
            "usage_provenance": None,
            "cost_provenance": None,
        }
    usage = result.get("usage")
    if isinstance(usage, dict):
        # A2A artifacts travel as protobuf Struct, which turns counts into floats.
        usage = {
            key: int(value) if isinstance(value, float) and value.is_integer() else value
            for key, value in usage.items()
        }
    return {
        "usage": usage,
        "cost_usd": result.get("cost_usd"),
        "usage_provenance": result.get("usage_provenance"),
        "cost_provenance": result.get("cost_provenance"),
    }


def write_manifest(repo: Path, payload: dict[str, Any]) -> Path:
    body = {"schema": MANIFEST_SCHEMA, "version": 2, **payload}
    run_id = str(body.get("run_id") or "")
    if not run_id:
        raise ValueError("manifest requires run_id")
    return write_json_atomic(manifest_path(repo, run_id), body)


def load_manifest(repo: Path, run_id: str) -> dict[str, Any] | None:
    path = manifest_path(repo, run_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
