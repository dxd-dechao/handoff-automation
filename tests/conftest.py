"""Sandbox limits for tests that are not marked ``server``.

``server`` tests need process probes (``ps``, ``kill 0`` on another PID), a
localhost bind or connect, a started service, or allocate a pseudo-terminal.
A probe run by a child ``handoff`` process counts too: this fixture only
patches the pytest process, so those tests are marked from inspection.
Everything else must pass with those denied and with ``TMPDIR`` under
pytest's ``tmp_path``.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

_LOCALHOST = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _host(address: object) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return ""


@pytest.fixture(autouse=True)
def sandbox_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tmpdir"
    path.mkdir()
    monkeypatch.setenv("TMPDIR", str(path))
    monkeypatch.setenv("TMP", str(path))
    monkeypatch.setenv("TEMP", str(path))


@pytest.fixture(autouse=True)
def deny_server_capabilities(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("server"):
        return
    real_connect = socket.socket.connect
    real_bind = socket.socket.bind
    real_run = subprocess.run
    real_popen = subprocess.Popen
    real_kill = os.kill

    def connect(self: socket.socket, address: object) -> object:
        if _host(address) in _LOCALHOST:
            raise PermissionError("simulated sandbox: localhost connect denied")
        return real_connect(self, address)  # type: ignore[arg-type]

    def bind(self: socket.socket, address: object) -> object:
        if _host(address) in _LOCALHOST:
            raise PermissionError("simulated sandbox: localhost bind denied")
        return real_bind(self, address)  # type: ignore[arg-type]

    def _blocked(cmd: object) -> bool:
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return False
        name = Path(str(cmd[0])).name
        return name == "ps"

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        if _blocked(cmd):
            raise PermissionError("simulated sandbox: ps denied")
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        cmd = args[0] if args else kwargs.get("args")
        if _blocked(cmd):
            raise PermissionError("simulated sandbox: ps denied")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    def kill(pid: int, sig: int) -> None:
        if pid not in {os.getpid(), 0}:
            raise PermissionError("simulated sandbox: kill denied")
        real_kill(pid, sig)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "kill", kill)
