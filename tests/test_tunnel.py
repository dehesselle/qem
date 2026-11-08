# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The ProxyCommand against a local WebSocket server standing in for the gateway."""

import os
import socket
import subprocess
import sys
import threading

import pytest
from websockets.sync.server import serve

from qem.errors import TunnelError
from qem.models import Access
from qem.tunnel import open_tunnel, tunnel_url

TOKEN = "t" * 40


@pytest.fixture
def gateway():
    seen = {}

    def handler(ws):
        seen["path"] = ws.request.path
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["agent"] = ws.request.headers.get("User-Agent", "").split("/")[0]
        seen["extensions"] = ws.request.headers.get("Sec-WebSocket-Extensions")
        for message in ws:
            ws.send(message[::-1])

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.socket.getsockname()[1]}", seen
        server.shutdown()


def run_tunnel(
    gateway_url: str, session_id: str = "ses_1", stdin: bytes = b""
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "QEM_TUNNEL_SESSION": session_id,
        "QEM_TUNNEL_GATEWAY": gateway_url,
        "QEM_TUNNEL_TOKEN": TOKEN,
    }
    return subprocess.run(
        [sys.executable, "-m", "qem", "proxy", session_id],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_bytes_pass_through(gateway):
    url, seen = gateway
    proc = subprocess.Popen(
        [sys.executable, "-m", "qem", "proxy", "ses_1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "QEM_TUNNEL_SESSION": "ses_1",
            "QEM_TUNNEL_GATEWAY": url,
            "QEM_TUNNEL_TOKEN": TOKEN,
        },
    )
    payload = bytes(range(256)) * 4  # every byte value, incl. \r\n and \x1a
    proc.stdin.write(payload)
    proc.stdin.flush()
    received = b""
    while len(received) < len(payload):
        received += os.read(proc.stdout.fileno(), 65536)
    proc.stdin.close()
    assert proc.wait(timeout=15) == 0, proc.stderr.read()
    assert received == payload[::-1]
    assert seen == {
        "path": "/api/v1/mac/sessions/ses_1/ssh",
        "auth": f"Bearer {TOKEN}",
        "agent": "qem",
        "extensions": None,
    }  # no compression offered


def test_unreachable_gateway_fails_cleanly():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    proc = run_tunnel(f"http://127.0.0.1:{port}")
    assert proc.returncode == 255
    assert proc.stdout == b""
    assert b"gateway" in proc.stderr


def test_refused_handshake_fails_cleanly():
    def refuse(connection, request):
        return connection.respond(401, "token expired\n")

    with serve(lambda ws: None, "127.0.0.1", 0, process_request=refuse) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.socket.getsockname()[1]}"
        access = Access(
            session_id="ses_1",
            generation=0,
            username="agent",
            host_key="",
            gateway=url,
            token=TOKEN,
        )
        with pytest.raises(TunnelError, match="refused the tunnel: HTTP 401") as exc:
            open_tunnel(access, open_timeout=10)
        assert TOKEN not in str(exc.value)
        proc = run_tunnel(url)
        server.shutdown()
    assert proc.returncode == 255 and proc.stdout == b""
    assert b"HTTP 401" in proc.stderr and TOKEN.encode() not in proc.stderr


@pytest.mark.parametrize(
    "gateway",
    [
        "http://gw.nomac.app",  # the token would travel in the clear
        "ws://gw.nomac.app",
        "http://10.0.0.1",
        "http://localhost.evil.example",
        "https://gw.nomac.app/path",
        "https://gw.nomac.app?x=1",
        "https://gw.nomac.app#x",
        "https://u:p@gw.nomac.app",
        "https://u@gw.nomac.app",
        "https://gw.nomac.app:99999",
        "https://",
        "",
        "gw.nomac.app",
        "ftp://x",
    ],
)
def test_gateway_must_be_https_origin(gateway):
    with pytest.raises(TunnelError, match="gateway"):
        tunnel_url(gateway, "ses_1")


@pytest.mark.parametrize(
    "gateway, url",
    [
        ("https://gw.nomac.app", "wss://gw.nomac.app/api/v1/mac/sessions/ses_1/ssh"),
        ("https://gw.nomac.app/", "wss://gw.nomac.app/api/v1/mac/sessions/ses_1/ssh"),
        (
            "https://gw.nomac.app:8443",
            "wss://gw.nomac.app:8443/api/v1/mac/sessions/ses_1/ssh",
        ),
        ("wss://gw.nomac.app", "wss://gw.nomac.app/api/v1/mac/sessions/ses_1/ssh"),
        ("http://127.0.0.1:9000", "ws://127.0.0.1:9000/api/v1/mac/sessions/ses_1/ssh"),
        ("http://localhost:9000", "ws://localhost:9000/api/v1/mac/sessions/ses_1/ssh"),
        ("http://[::1]:9000", "ws://[::1]:9000/api/v1/mac/sessions/ses_1/ssh"),
    ],
)
def test_tunnel_url(gateway, url):
    assert tunnel_url(gateway, "ses_1") == url


def test_session_id_cannot_change_the_path():
    assert tunnel_url("https://gw", "../x?y#z").endswith("/sessions/..%2Fx%3Fy%23z/ssh")
    with pytest.raises(TunnelError):
        tunnel_url("https://gw", "")
