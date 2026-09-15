from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from handoff_a2a.processes import group_pids, start_owned, stop_owned


def _wait_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _write_orphan_parent(path: Path) -> None:
    path.write_text(
        "import os, pathlib, subprocess, sys\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "pid_file = pathlib.Path(sys.argv[2])\n"
        "child_src = pathlib.Path(sys.argv[3])\n"
        "child_src.write_text(\n"
        "    'import pathlib, signal, sys, time\\n'\n"
        "    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
        "    'p = pathlib.Path(sys.argv[1])\\n'\n"
        "    'while True:\\n'\n"
        "    '    p.write_text((p.read_text() if p.exists() else \\\"\\\") + \\\"x\\\")\\n'\n"
        "    '    time.sleep(0.05)\\n'\n"
        ")\n"
        "child = subprocess.Popen([sys.executable, str(child_src), str(marker)])\n"
        "pid_file.write_text(str(child.pid))\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )


def test_stop_owned_kills_term_immune_child_after_parent_exits(tmp_path: Path) -> None:
    parent = tmp_path / "parent.py"
    child_src = tmp_path / "child.py"
    marker = tmp_path / "writes"
    pid_file = tmp_path / "child.pid"
    _write_orphan_parent(parent)
    owned = start_owned(
        [sys.executable, str(parent), str(marker), str(pid_file), str(child_src)],
        cwd=tmp_path,
        env=os.environ,
    )
    try:
        _wait_file(pid_file)
        child_pid = int(pid_file.read_text().strip())
        deadline = time.time() + 3
        while time.time() < deadline and owned.process.poll() is None:
            time.sleep(0.02)
        assert owned.process.poll() is not None
        os.kill(child_pid, 0)
        assert child_pid in group_pids(owned.pgid)
        assert stop_owned(owned, grace_s=0.4) is True
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert child_pid not in group_pids(owned.pgid)
    finally:
        if pid_file.is_file():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_denied_killpg_does_not_treat_parent_exit_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent.py"
    child_src = tmp_path / "child.py"
    marker = tmp_path / "writes"
    pid_file = tmp_path / "child.pid"
    _write_orphan_parent(parent)
    owned = start_owned(
        [sys.executable, str(parent), str(marker), str(pid_file), str(child_src)],
        cwd=tmp_path,
        env=os.environ,
    )
    child_pid = None
    try:
        _wait_file(pid_file)
        child_pid = int(pid_file.read_text().strip())
        deadline = time.time() + 3
        while time.time() < deadline and owned.process.poll() is None:
            time.sleep(0.02)
        assert owned.process.poll() is not None
        os.kill(child_pid, 0)

        def deny(pgid: int, sig: int) -> None:
            raise PermissionError("group signaling denied")

        monkeypatch.setattr(os, "killpg", deny)
        assert stop_owned(owned, grace_s=0.2) is False
        os.kill(child_pid, 0)
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
