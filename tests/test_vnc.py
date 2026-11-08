# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import os
import socket
import sys
import types

import pytest

from qem import TransferError, vnc
from qem.models import Login

LOGIN = Login(user="agent", password="p@ss:w/rd")

# Stands in for `ssh -N -L PORT:...`: opens the local port, then idles until killed.
LISTEN = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
    "time.sleep(60)\n"
)


class FakePomonaError(Exception):
    pass


class FakeTunnel:
    def __init__(self, port, alive=True):
        self.port, self.alive, self.closed = port, alive, False

    def close(self):
        self.closed = True


@pytest.fixture
def pomona(monkeypatch):
    """A stand-in for the pomona package; `window` runs in place of run_window."""
    fake = types.ModuleType("pomona")
    fake.PomonaError = FakePomonaError
    fake.connects = []
    fake.window = lambda session, reconnect: session

    def connect(host, port, **kw):
        fake.connects.append((host, port, kw))
        return types.SimpleNamespace(port=port, error=None)

    def run_window(session, *, title, reconnect):
        fake.title = title
        return fake.window(session, reconnect)

    fake.connect, fake.run_window = connect, run_window
    monkeypatch.setitem(sys.modules, "pomona", fake)
    return fake


def test_show_desktop_signs_in_through_the_tunnel(pomona):
    vnc.show_desktop(FakeTunnel(5901), LOGIN, title="NoMac ses_1")
    assert pomona.connects == [
        ("127.0.0.1", 5901, {"user": "agent", "password": "p@ss:w/rd", "picture": None})
    ]
    assert pomona.title == "NoMac ses_1"


def test_show_desktop_reopens_a_dead_tunnel(pomona):
    first, reopened = FakeTunnel(5901), []

    def reopen():
        reopened.append(FakeTunnel(5901))
        return reopened[-1]

    def window(session, reconnect):
        assert reconnect("pic").port == 5901 and not reopened  # ssh still up
        first.alive = False
        return reconnect("pic")

    pomona.window = window
    session = vnc.show_desktop(first, LOGIN, reopen=reopen)
    assert session is None  # returns nothing; the window's session stays inside
    assert len(reopened) == 1 and reopened[0].closed and not first.closed
    assert pomona.connects[-1][2]["picture"] == "pic"


def test_show_desktop_retries_a_tunnel_that_cannot_reopen(pomona):
    def reopen():
        raise TransferError("ssh ended before the forward was up")

    def window(session, reconnect):
        with pytest.raises(OSError, match="ssh ended"):  # pomona retries an OSError
            reconnect("pic")
        return session

    pomona.window = window
    vnc.show_desktop(FakeTunnel(5901, alive=False), LOGIN, reopen=reopen)


def test_show_desktop_reports_failures(pomona):
    def refused(host, port, **kw):
        raise FakePomonaError("the Mac refused the sign-in")

    pomona.connect = refused
    with pytest.raises(TransferError, match="could not sign in.*refused"):
        vnc.show_desktop(FakeTunnel(5901), LOGIN)

    pomona.connect = lambda host, port, **kw: types.SimpleNamespace(error=None)
    pomona.window = lambda session, reconnect: types.SimpleNamespace(error="hung up")
    with pytest.raises(TransferError, match="session ended: hung up"):
        vnc.show_desktop(FakeTunnel(5901), LOGIN)


def test_login_hides_password():
    login = Login.from_json(
        {
            "session_id": "ses_1",
            "user": "agent",
            "password": "secret",
            "variable": "NOMAC_MAC_PASSWORD",
        }
    )
    assert (login.user, login.password, login.variable) == (
        "agent",
        "secret",
        "NOMAC_MAC_PASSWORD",
    )
    assert "secret" not in repr(login)


def test_free_port_and_listening():
    port = vnc.free_port()
    assert not vnc.listening(port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        assert vnc.listening(port)


def test_open_tunnel_waits_for_the_port_and_closes():
    port = vnc.free_port()
    with vnc.open_tunnel(
        [sys.executable, "-c", LISTEN, str(port)], dict(os.environ), port, timeout=20
    ) as tunnel:
        assert tunnel.alive and vnc.listening(port)
    assert not tunnel.alive


def test_open_tunnel_reports_ssh_failure():
    port = vnc.free_port()
    fail = (
        "import sys; sys.stderr.write('Permission denied (publickey).'); sys.exit(255)"
    )
    with pytest.raises(TransferError, match=r"code 255\): Permission denied"):
        vnc.open_tunnel(
            [sys.executable, "-c", fail], dict(os.environ), port, timeout=20
        )


def test_open_tunnel_times_out():
    port = vnc.free_port()
    now = iter(range(0, 1000, 5))
    with pytest.raises(TransferError, match="no forward after 20 s"):
        vnc.open_tunnel(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            dict(os.environ),
            port,
            timeout=20,
            sleep=lambda s: None,
            clock=lambda: next(now),
        )


def test_open_tunnel_refuses_a_busy_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(TransferError, match="already in use"):
            vnc.open_tunnel(
                [sys.executable, "-c", LISTEN, str(port)], dict(os.environ), port
            )


def test_session_desktop_tunnel(api, make_client, monkeypatch, capsys):
    api.on(
        "POST",
        "/mac/sessions/ses_1/access",
        {
            "session_id": "ses_1",
            "ssh": {"host_key": "ssh-ed25519 HOSTKEY"},
            "gateway": "https://gw",
            "token": "tok",
        },
    )
    seen = {}

    def fake_ssh_argv(session_id, identity, command=None, *, forward=None):
        seen["forward"] = forward
        return [sys.executable, "-c", LISTEN, str(forward[0])]

    monkeypatch.setattr("qem.ssh.ssh_argv", fake_ssh_argv)
    with make_client().session("ses_1").desktop_tunnel() as tunnel:
        assert seen["forward"] == (tunnel.port, 5900)
        assert vnc.listening(tunnel.port)
    assert capsys.readouterr() == ("", "")
