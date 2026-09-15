"""Owned worker processes: one POSIX process group per execution."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


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


def process_start_identity(pid: int) -> str:
    """Stable-enough identity so we never kill a recycled PID."""
    try:
        stat = os.stat(f"/proc/{pid}")
        ctime = getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))
        return f"{pid}:{ctime}"
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
        stamp = result.stdout.strip()
        if result.returncode == 0 and stamp:
            return f"{pid}:{stamp}"
    except OSError:
        pass
    return f"{pid}:unknown"


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
    return True


def identity_matches(owned: OwnedProcess) -> bool:
    if not pid_exists(owned.pid):
        return False
    return process_start_identity(owned.pid) == owned.start_identity


def is_owned_alive(owned: OwnedProcess) -> bool:
    if owned.pid <= 0:
        return False
    if owned.process is not None and owned.process.poll() is not None:
        return False
    return identity_matches(owned)


def stop_owned(owned: OwnedProcess, *, grace_s: float) -> bool:
    """Signal the owned group, wait, escalate, and reap. True if confirmed stopped."""
    if owned.pid <= 0:
        return True
    if pid_exists(owned.pid) and not identity_matches(owned):
        return False
    if not is_owned_alive(owned):
        _reap(owned)
        return True
    try:
        _signal_owned(owned, signal.SIGTERM)
    except ProcessLookupError:
        _reap(owned)
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + max(grace_s, 0)
    while time.monotonic() < deadline:
        if not is_owned_alive(owned):
            _reap(owned)
            return True
        time.sleep(0.05)
    try:
        _signal_owned(owned, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return not is_owned_alive(owned)
    _reap(owned)
    return not is_owned_alive(owned)


def _signal_owned(owned: OwnedProcess, sig: int) -> None:
    """Signal the verified group; fall back to the owned PID if killpg is denied."""
    try:
        os.killpg(owned.pgid, sig)
    except PermissionError:
        os.kill(owned.pid, sig)


def wait_owned(owned: OwnedProcess, *, timeout_s: float) -> int | None:
    if owned.process is None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not is_owned_alive(owned):
                return 0
            time.sleep(0.05)
        return None
    try:
        return int(owned.process.wait(timeout=timeout_s))
    except subprocess.TimeoutExpired:
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
