# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The Mac's desktop, in a pomona window.

The Mac runs macOS Screen Sharing on 127.0.0.1:5900, which only ssh reaches.
A local port forward through the session's tunnel makes it reachable on this
computer. pomona signs in there with the session's login (RFB security type
30), keeps it in memory, and shows the screen in Apple's Adaptive quality.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Self

from .errors import NomacError, TransferError
from .models import Login

if TYPE_CHECKING:
    import pomona

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


def address(port: int) -> str:
    """HOST::PORT, the form VNC viewers take for a full port number."""
    return f"127.0.0.1::{port}"


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
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
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


def show_desktop(
    tunnel: DesktopTunnel,
    login: Login,
    *,
    title: str | None = None,
    reopen: Callable[[], DesktopTunnel] | None = None,
) -> None:
    """Show the Mac's desktop through `tunnel` in a pomona window, until it closes.

    Requirements: sign in to 127.0.0.1:<tunnel.port> with `login`. When the
    connection drops, pomona signs in again; if ssh has ended by then and
    `reopen` is given, `reopen()` replaces the tunnel first. Tunnels opened
    that way are closed before returning; `tunnel` stays the caller's. Raise
    TransferError when the first sign-in fails or the session ended with an
    error, and pass on pomona's RuntimeError when no window can be opened.
    """
    import pomona  # loads the Rust extension and cryptography; only the desktop needs them

    current = tunnel
    opened: list[DesktopTunnel] = []

    def sign_in(picture: object = None) -> pomona.Session:
        return pomona.connect(
            "127.0.0.1",
            current.port,
            user=login.user,
            password=login.password,
            picture=picture,
        )

    def reconnect(picture: object) -> pomona.Session:
        nonlocal current
        if reopen is not None and not current.alive:
            try:
                current = reopen()
            except NomacError as e:
                # pomona tries again only after an OSError or a dropped connection.
                raise OSError(str(e)) from e
            opened.append(current)
        return sign_in(picture)

    try:
        try:
            session = sign_in()
        except (OSError, pomona.PomonaError) as e:
            raise TransferError(f"could not sign in to Screen Sharing: {e}") from e
        session = pomona.run_window(session, title=title, reconnect=reconnect)
    finally:
        for t in opened:
            t.close()
    if session.error is not None:
        raise TransferError(f"the desktop session ended: {session.error}")
