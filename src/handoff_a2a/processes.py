"""Owned worker processes: one POSIX process group per execution."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# alive: identity matches a live process. dead: ProcessLookupError or a
# positively different start identity. unknown: the probe itself was denied.
LIVENESS_ALIVE = "alive"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"


@dataclass
class OwnedProcess:
    process: subprocess.Popen[bytes]
    pid: int
    pgid: int
    start_identity: str
    command: tuple[str, ...]
    cwd: Path


def start_owned(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> OwnedProcess:
    out = stdout_path.open("wb") if stdout_path is not None else subprocess.DEVNULL
    err = stderr_path.open("wb") if stderr_path is not None else subprocess.DEVNULL
    try:
        child_env = dict(env)
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    finally:
        if stdout_path is not None:
            out.close()
        if stderr_path is not None:
            err.close()
    pgid = os.getpgid(process.pid)
    return OwnedProcess(
        process=process,
        pid=process.pid,
        pgid=pgid,
        start_identity=process_start_identity(process.pid),
        command=tuple(command),
        cwd=cwd,
    )


def permission_denied(exc: BaseException | None) -> bool:
    """True when this exception, or a cause of it, is EPERM/EACCES."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc] if exc is not None else []
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        if isinstance(current, OSError) and current.errno in (errno.EPERM, errno.EACCES):
            return True
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None and not current.__suppress_context__:
            stack.append(current.__context__)
        nested = getattr(current, "exceptions", None)
        if nested:
            stack.extend(nested)
    return False


def _ps_denied(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stderr or ''} {result.stdout or ''}"
    lowered = text.lower()
    return "operation not permitted" in lowered or "not permitted" in lowered


def read_start_identity(pid: int) -> tuple[str | None, bool]:
    """(identity, permitted). permitted is False when ps//proc could not be used."""
    try:
        stat = os.stat(f"/proc/{pid}")
        ctime = getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))
        return f"{pid}:{ctime}", True
    except OSError as exc:
        if permission_denied(exc):
            return None, False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, False
    if result.returncode == 0 and result.stdout.strip():
        return f"{pid}:{result.stdout.strip()}", True
    if _ps_denied(result):
        return None, False
    # ps ran and did not identify the process (gone, or no stamp).
    return None, True


def process_start_identity(pid: int) -> str:
    """Stable-enough identity so we never kill a recycled PID.

    A denied probe returns `{pid}:unknown` only as a placeholder for the
    caller that just spawned the process; liveness checks use
    `process_liveness` and never treat that placeholder as a mismatch.
    """
    identity, _permitted = read_start_identity(pid)
    if identity:
        return identity
    return f"{pid}:unknown"


def process_liveness(pid: int, expected_identity: str) -> str:
    """alive, dead, or unknown. Unknown is never stale and never a mismatch."""
    if pid <= 0:
        return LIVENESS_DEAD
    kill_denied = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LIVENESS_DEAD
    except OSError as exc:
        if permission_denied(exc):
            kill_denied = True
        elif exc.errno == errno.ESRCH:
            return LIVENESS_DEAD
        else:
            return LIVENESS_UNKNOWN
    identity, permitted = read_start_identity(pid)
    if not permitted or identity is None:
        if kill_denied or not permitted:
            return LIVENESS_UNKNOWN
        # ps ran, process exists (kill succeeded), but no start stamp.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return LIVENESS_DEAD
        except OSError:
            return LIVENESS_UNKNOWN
        return LIVENESS_UNKNOWN
    if identity.endswith(":unknown"):
        return LIVENESS_UNKNOWN
    if identity != expected_identity:
        return LIVENESS_DEAD
    if _is_zombie(pid):
        return LIVENESS_DEAD
    return LIVENESS_ALIVE


def owned_from_record(*, pid: int, pgid: int, start_identity: str, cwd: Path) -> OwnedProcess:
    return OwnedProcess(
        process=None,  # type: ignore[arg-type]
        pid=pid,
        pgid=pgid,
        start_identity=start_identity,
        command=(),
        cwd=cwd,
    )


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    state = result.stdout.strip()
    return bool(state) and state[0].upper() == "Z"


def identity_matches(owned: OwnedProcess) -> bool:
    return process_liveness(owned.pid, owned.start_identity) == LIVENESS_ALIVE


def group_pids(pgid: int) -> frozenset[int]:
    """Live (non-zombie) PIDs currently in this process group."""
    if pgid <= 0:
        return frozenset()
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,pgid=,state="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return frozenset()
    found: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            group = int(parts[1])
        except ValueError:
            continue
        state = parts[2]
        if group == pgid and (not state or state[0].upper() != "Z"):
            found.add(pid)
    return frozenset(found)


def is_owned_alive(owned: OwnedProcess) -> bool:
    """True if owned work may still be running, including group descendants after the leader exits."""
    if owned.pid <= 0:
        return False
    if pid_exists(owned.pid) and not identity_matches(owned):
        return True
    if identity_matches(owned):
        return True
    return bool(group_pids(owned.pgid))


def stop_owned(owned: OwnedProcess, *, grace_s: float) -> bool:
    """Signal the owned group, wait, escalate, and reap. True only if the group is empty."""
    if owned.pid <= 0:
        return True
    if pid_exists(owned.pid) and not identity_matches(owned):
        return False
    if not is_owned_alive(owned):
        _reap(owned)
        return True
    if _signal_group(owned.pgid, signal.SIGTERM) == "denied":
        return not is_owned_alive(owned)
    deadline = time.monotonic() + max(grace_s, 0)
    while time.monotonic() < deadline:
        if not is_owned_alive(owned):
            _reap(owned)
            return True
        time.sleep(0.05)
    signaled = _signal_group(owned.pgid, signal.SIGKILL)
    if signaled == "denied":
        return not is_owned_alive(owned)
    kill_deadline = time.monotonic() + min(max(grace_s, 0.2), 2.0)
    while time.monotonic() < kill_deadline:
        if not is_owned_alive(owned):
            _reap(owned)
            return True
        time.sleep(0.05)
    _reap(owned)
    return not is_owned_alive(owned)


def _signal_group(pgid: int, sig: int) -> str:
    """Signal every member of the verified group. Never falls back to an unverified PID."""
    try:
        os.killpg(pgid, sig)
        return "ok"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"


def wait_owned(owned: OwnedProcess, *, timeout_s: float) -> int | None:
    """Wait until the owned group has no remaining members, not only the parent PID."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if owned.process is not None:
            owned.process.poll()
        if not is_owned_alive(owned):
            _reap(owned)
            if owned.process is not None and owned.process.returncode is not None:
                return int(owned.process.returncode)
            return 0
        time.sleep(0.05)
    return None


def _reap(owned: OwnedProcess) -> None:
    if owned.process is not None:
        try:
            owned.process.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        os.waitpid(owned.pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return
