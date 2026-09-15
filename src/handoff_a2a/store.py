"""SQLite TaskStore plus execution claims. One database per server process."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks.task_store import TaskStore
from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict, ParseDict

from handoff_a2a.contracts import ContractError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    caller_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    task_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    pgid INTEGER,
    start_identity TEXT,
    evidence_dir TEXT,
    result_json TEXT,
    recovery_required INTEGER NOT NULL DEFAULT 0,
    recovery_reason TEXT,
    PRIMARY KEY (caller_id, execution_id)
);
"""

NONTERMINAL = frozenset({"claimed", "running", "recovery_required"})


@dataclass
class ExecutionClaim:
    caller_id: str
    execution_id: str
    request_hash: str
    request_json: str
    task_id: str
    context_id: str
    run_id: str
    workflow_id: str
    status: str
    created: bool
    pid: int | None = None
    pgid: int | None = None
    start_identity: str | None = None
    evidence_dir: str | None = None
    result_json: str | None = None
    recovery_required: bool = False
    recovery_reason: str | None = None


class ClaimConflict(ContractError):
    pass


def task_to_json(task: Task) -> str:
    return json.dumps(MessageToDict(task), default=str)


def task_from_json(text: str) -> Task:
    return ParseDict(json.loads(text), Task())


class SqliteState:
    def __init__(self, path: Path, caller_id: str):
        self.path = path
        self.caller_id = caller_id
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim(
        self,
        *,
        execution_id: str,
        request_hash: str,
        request: dict[str, Any],
        run_id: str,
        workflow_id: str,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> ExecutionClaim:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ? AND execution_id = ?",
                (self.caller_id, execution_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ClaimConflict(
                        "execution_id already used with a different request"
                    )
                return self._row_to_claim(existing, created=False)
            assigned_task = task_id or str(uuid.uuid4())
            assigned_context = context_id or str(uuid.uuid4())
            payload = json.dumps(request, sort_keys=True)
            self._conn.execute(
                """INSERT INTO executions (
                    caller_id, execution_id, request_hash, request_json,
                    task_id, context_id, run_id, workflow_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed')""",
                (
                    self.caller_id,
                    execution_id,
                    request_hash,
                    payload,
                    assigned_task,
                    assigned_context,
                    run_id,
                    workflow_id,
                ),
            )
            stub = Task(
                id=assigned_task,
                context_id=assigned_context,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
            self._conn.execute(
                """INSERT INTO tasks (task_id, caller_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET payload = excluded.payload""",
                (assigned_task, self.caller_id, task_to_json(stub)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ? AND execution_id = ?",
                (self.caller_id, execution_id),
            ).fetchone()
            return self._row_to_claim(row, created=True)

    def get_execution(self, execution_id: str) -> ExecutionClaim | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ? AND execution_id = ?",
                (self.caller_id, execution_id),
            ).fetchone()
            return None if row is None else self._row_to_claim(row, created=False)

    def get_by_task(self, task_id: str) -> ExecutionClaim | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ? AND task_id = ?",
                (self.caller_id, task_id),
            ).fetchone()
            return None if row is None else self._row_to_claim(row, created=False)

    def list_nonterminal(self) -> list[ExecutionClaim]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ? AND status IN ('claimed', 'running', 'recovery_required')",
                (self.caller_id,),
            ).fetchall()
            return [self._row_to_claim(row, created=False) for row in rows]

    def list_all(self) -> list[ExecutionClaim]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE caller_id = ?",
                (self.caller_id,),
            ).fetchall()
            return [self._row_to_claim(row, created=False) for row in rows]

    def try_dispatch(self, execution_id: str) -> bool:
        """Mark a claimed execution running. Only one caller wins."""
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE executions SET status = 'running'
                   WHERE caller_id = ? AND execution_id = ? AND status = 'claimed'""",
                (self.caller_id, execution_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def update_execution(self, execution_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status",
            "pid",
            "pgid",
            "start_identity",
            "evidence_dir",
            "result_json",
            "recovery_required",
            "recovery_reason",
        }
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update {key}")
            if key == "recovery_required":
                value = 1 if value else 0
            if key == "result_json" and not isinstance(value, str) and value is not None:
                value = json.dumps(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        values.extend([self.caller_id, execution_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE executions SET {', '.join(assignments)} WHERE caller_id = ? AND execution_id = ?",
                values,
            )
            self._conn.commit()

    def save_task(self, task: Task) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO tasks (task_id, caller_id, payload) VALUES (?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET payload = excluded.payload""",
                (task.id, self.caller_id, task_to_json(task)),
            )
            self._conn.commit()

    def load_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM tasks WHERE task_id = ? AND caller_id = ?",
                (task_id, self.caller_id),
            ).fetchone()
            return None if row is None else task_from_json(row["payload"])

    def list_tasks(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM tasks WHERE caller_id = ?",
                (self.caller_id,),
            ).fetchall()
            return [task_from_json(row["payload"]) for row in rows]

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM tasks WHERE task_id = ? AND caller_id = ?",
                (task_id, self.caller_id),
            )
            self._conn.commit()

    def _row_to_claim(self, row: sqlite3.Row, *, created: bool) -> ExecutionClaim:
        return ExecutionClaim(
            caller_id=row["caller_id"],
            execution_id=row["execution_id"],
            request_hash=row["request_hash"],
            request_json=row["request_json"],
            task_id=row["task_id"],
            context_id=row["context_id"],
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            status=row["status"],
            created=created,
            pid=row["pid"],
            pgid=row["pgid"],
            start_identity=row["start_identity"],
            evidence_dir=row["evidence_dir"],
            result_json=row["result_json"],
            recovery_required=bool(row["recovery_required"]),
            recovery_reason=row["recovery_reason"],
        )


class SqliteTaskStore(TaskStore):
    def __init__(self, state: SqliteState):
        self._state = state

    async def save(self, task: Task, context: ServerCallContext) -> None:
        self._state.save_task(task)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return self._state.load_task(task_id)

    async def list(
        self,
        params: ListTasksRequest,
        context: ServerCallContext,
    ) -> ListTasksResponse:
        tasks = self._state.list_tasks()
        if params.context_id:
            tasks = [task for task in tasks if task.context_id == params.context_id]
        return ListTasksResponse(tasks=tasks, total_size=len(tasks), page_size=len(tasks))

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        self._state.delete_task(task_id)
