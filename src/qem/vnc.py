# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The Mac's desktop over VNC.

The Mac runs macOS Screen Sharing on 127.0.0.1:5900, which only ssh reaches.
A local port forward through the session's tunnel makes it a VNC server on
this computer. Screen Sharing signs in with the session's login (RFB security
type 30, which TigerVNC calls DH). TigerVNC reads that login from
VNC_USERNAME and VNC_PASSWORD, so it never lands on disk or in argv.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Self
from urllib.parse import quote

from .errors import TransferError
from .models import Login

SCREEN_SHARING_PORT = 5900
POLL_INTERVAL = 0.25  # how often open_tunnel checks whether the forward is up


def free_port() -> int:
    """A TCP port on 127.0.0.1 that is free at the moment of the call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))  # the system picks an unused port
        return s.getsockname()[1]


def _bindable(port: int) -> bool:
    """Whether ssh could listen on 127.0.0.1:`port` now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # As ssh does; on Windows the option would let us share a busy port.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def find_viewer() -> str | None:
    """TigerVNC's vncviewer on PATH, or where its Windows installer puts it."""
    if found := shutil.which("vncviewer"):
        return found
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if root and (exe := Path(root) / "TigerVNC" / "vncviewer.exe").is_file():
                return str(exe)
    return None


def address(port: int) -> str:
    """HOST::PORT, the form VNC viewers take for a full port number."""
    return f"127.0.0.1::{port}"


def viewer_argv(viewer: str, port: int) -> list[str]:
    return [viewer, address(port)]


def viewer_env(login: Login) -> dict[str, str]:
    return {"VNC_USERNAME": login.user, "VNC_PASSWORD": login.password}


def vnc_url(login: Login, port: int) -> str:
    """vnc://user:password@127.0.0.1:port, for macOS Screen Sharing (`open`)."""
    return f"vnc://{quote(login.user, safe='')}:{quote(login.password, safe='')}@127.0.0.1:{port}"


class DesktopTunnel:
    """An ssh process forwarding 127.0.0.1:`port` to the Mac's Screen Sharing.

    Use as a context manager, or call close(), so ssh does not outlive it.
    """

    def __init__(self, process: subprocess.Popen, port: int):
        self.process = process
        self.port = port

    def __repr__(self) -> str:
        return f"DesktopTunnel(port={self.port}, alive={self.alive})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout)

    def close(self) -> None:
        if self.alive:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def open_tunnel(
    argv: Sequence[str],
    env: dict[str, str],
    port: int,
    *,
    timeout: float = 20,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DesktopTunnel:
    """Start the ssh forward `argv` (with `env`) and return once `port` accepts connections.

    Requirements: refuse at once if `port` is already in use. ssh opens the local
    port only after it has signed in, so poll `listening(port)`. If ssh exits
    first, or `timeout` seconds pass, close the tunnel and raise TransferError
    saying which of the two happened (for the timeout: "no forward after
    <timeout> s"), with ssh's stderr appended. ssh's stdout is not used; stdin is
    closed. Nothing is left running on failure.
    """
    if listening(port) or not _bindable(port):
        raise TransferError(f"port {port} on 127.0.0.1 is already in use")
    # ssh's stderr goes to a file: a pipe nobody reads could fill and stall ssh.
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
            )
        except OSError as e:
            raise TransferError(f"ssh could not start: {e}") from e
        tunnel = DesktopTunnel(process, port)
        try:
            deadline = clock() + timeout
            while True:
                if (code := process.poll()) is not None:
                    failure = f"ssh ended before the forward was up (exit code {code})"
                    break
                if listening(port):
                    return tunnel
                if clock() >= deadline:
                    failure = f"no forward after {timeout:g} s"
                    break
                sleep(POLL_INTERVAL)
        except BaseException:
            tunnel.close()
            raise
        tunnel.close()
        errors.seek(0)
        detail = errors.read().decode(errors="replace").strip()
    raise TransferError(failure + (f": {detail}" if detail else ""))
