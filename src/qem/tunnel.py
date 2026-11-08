# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""SSH over a WebSocket to the NoMac gateway.

The gateway endpoint is wss://<gateway>/api/v1/mac/sessions/<id>/ssh with the
short-lived access token as Bearer. Every binary frame is raw SSH bytes; there
is no handshake of its own and no reconnect.
"""

from __future__ import annotations

import ipaddress
import os
import threading
from urllib.parse import quote, urlsplit

from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidHandshake,
    InvalidStatus,
    WebSocketException,
)
from websockets.sync.client import ClientConnection, connect

from .api import _user_agent
from .errors import TunnelError
from .models import Access

MAX_FRAME = 1024 * 1024
READ_SIZE = 64 * 1024

# Lets a parent that already called /access hand the token to the ProxyCommand
# child it spawns through ssh, instead of the child calling /access again.
ENV_SESSION = "QEM_TUNNEL_SESSION"
ENV_GATEWAY = "QEM_TUNNEL_GATEWAY"
ENV_TOKEN = "QEM_TUNNEL_TOKEN"


def tunnel_env(access: Access) -> dict[str, str]:
    return {
        ENV_SESSION: access.session_id,
        ENV_GATEWAY: access.gateway,
        ENV_TOKEN: access.token,
    }


def access_from_env(session_id: str) -> Access | None:
    if os.environ.get(ENV_SESSION) != session_id or not os.environ.get(ENV_TOKEN):
        return None
    return Access(
        session_id=session_id,
        generation=0,
        username="agent",
        host_key="",
        gateway=os.environ.get(ENV_GATEWAY, ""),
        token=os.environ[ENV_TOKEN],
    )


def tunnel_url(gateway: str, session_id: str) -> str:
    """The WebSocket URL for a session's ssh tunnel.

    Fact (docs/protocol.md): wss://<gateway host>/api/v1/mac/sessions/<id>/ssh,
    where `gateway` is the origin the /access call returned.
    Requirements: the Bearer token sent there must never travel unencrypted to a
    remote host, and must only go to the gateway host the API named. A plain
    http gateway is acceptable only on this computer (tests use local servers).
    Anything unusable raises TunnelError naming the bad value.
    """
    if not session_id:
        raise TunnelError("no session id for the tunnel")
    try:
        parts = urlsplit(gateway)
        host, port = parts.hostname, parts.port
    except ValueError as e:
        raise TunnelError(f"unusable gateway {gateway!r}: {e}") from e
    if not host:
        raise TunnelError(f"unusable gateway {gateway!r}: no host")
    if parts.username is not None or parts.password is not None:
        raise TunnelError(f"unusable gateway {gateway!r}: it carries a login")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise TunnelError(
            f"unusable gateway {gateway!r}: not an origin (scheme://host[:port])"
        )
    if parts.scheme in ("https", "wss"):
        scheme = "wss"
    elif parts.scheme in ("http", "ws"):
        if not _loopback(host):
            raise TunnelError(
                f"unusable gateway {gateway!r}: the token would travel unencrypted"
            )
        scheme = "ws"
    else:
        raise TunnelError(f"unusable gateway {gateway!r}: scheme must be https")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    # Quoted, so no session id can lead the request to another path.
    return f"{scheme}://{netloc}/api/v1/mac/sessions/{quote(session_id, safe='')}/ssh"


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def open_tunnel(access: Access, *, open_timeout: float = 15) -> ClientConnection:
    """Open the WebSocket for `access` (websockets' sync client).

    Facts: Authorization: Bearer <access.token>; binary frames carry raw SSH
    bytes; no subprotocol, no compression; frames up to 1 MiB were seen.
    Send our User-Agent (`_user_agent()`). ssh does its own keepalives.
    A refused handshake or an unreachable gateway raises TunnelError.
    """
    url = tunnel_url(access.gateway, access.session_id)
    if not access.token:
        raise TunnelError("no access token for the tunnel")
    gateway = access.gateway
    try:
        return connect(
            url,
            additional_headers={"Authorization": f"Bearer {access.token}"},
            user_agent_header=_user_agent(),
            compression=None,
            # Plain ws:// only ever goes to this computer; never via a proxy.
            proxy=True if url.startswith("wss:") else None,
            open_timeout=open_timeout,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            # Headroom over the largest frames seen, so one never ends the session.
            max_size=4 * MAX_FRAME,
            legacy=True,  # we keep the connection open past this call
        )
    except InvalidStatus as e:
        raise TunnelError(
            f"gateway {gateway} refused the tunnel: HTTP {e.response.status_code}"
        ) from e
    except (InvalidHandshake, WebSocketException, ValueError) as e:
        # ValueError: websockets refuses some redirects with it.
        raise TunnelError(f"gateway {gateway} refused the tunnel: {e}") from e
    except OSError as e:  # includes TimeoutError and TLS errors
        raise TunnelError(f"gateway {gateway} is unreachable: {e}") from e


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def pump(ws: ClientConnection, stdin_fd: int = 0, stdout_fd: int = 1) -> None:
    """Copy bytes both ways until either side closes. Only SSH bytes reach stdout_fd."""

    def upstream() -> None:
        try:
            while data := os.read(stdin_fd, READ_SIZE):
                ws.send(data)
        except (OSError, ConnectionClosed):
            pass
        finally:
            ws.close()

    threading.Thread(target=upstream, name="qem-tunnel-up", daemon=True).start()
    try:
        for message in ws:
            _write_all(
                stdout_fd, message if isinstance(message, bytes) else message.encode()
            )
    except ConnectionClosedError as e:
        raise TunnelError(
            f"tunnel disconnected ({e}); an SSH connection cannot be resumed"
        ) from e
    except OSError as e:
        # ssh closed our stdout: it is done with us.
        ws.close()
        if e.errno not in (32, 22):  # EPIPE, EINVAL (Windows)
            raise TunnelError(f"tunnel output failed: {e}") from e


def run_tunnel(access: Access, stdin_fd: int = 0, stdout_fd: int = 1) -> None:
    pump(open_tunnel(access), stdin_fd, stdout_fd)
