# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import os
import socket
import sys

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


def test_viewer_command_and_login():
    assert vnc.viewer_argv("vncviewer", 5901) == ["vncviewer", "127.0.0.1::5901"]
    assert vnc.viewer_env(LOGIN) == {"VNC_USERNAME": "agent", "VNC_PASSWORD": "p@ss:w/rd"}
    assert vnc.vnc_url(LOGIN, 5901) == "vnc://agent:p%40ss%3Aw%2Frd@127.0.0.1:5901"


def test_login_hides_password():
    login = Login.from_json({"session_id": "ses_1", "user": "agent", "password": "secret", "variable": "NOMAC_MAC_PASSWORD"})
    assert (login.user, login.password, login.variable) == ("agent", "secret", "NOMAC_MAC_PASSWORD")
    assert "secret" not in repr(login)


def test_free_port_and_listening():
    port = vnc.free_port()
    assert not vnc.listening(port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        assert vnc.listening(port)


def test_find_viewer(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/bin/vncviewer" if name == "vncviewer" else None)
    assert vnc.find_viewer() == "/opt/bin/vncviewer"

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert vnc.find_viewer() is None
    exe = tmp_path / "TigerVNC" / "vncviewer.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert vnc.find_viewer() == str(exe)


def test_open_tunnel_waits_for_the_port_and_closes():
    port = vnc.free_port()
    with vnc.open_tunnel([sys.executable, "-c", LISTEN, str(port)], dict(os.environ), port, timeout=20) as tunnel:
        assert tunnel.alive and vnc.listening(port)
    assert not tunnel.alive


def test_open_tunnel_reports_ssh_failure():
    port = vnc.free_port()
    fail = "import sys; sys.stderr.write('Permission denied (publickey).'); sys.exit(255)"
    with pytest.raises(TransferError, match=r"code 255\): Permission denied"):
        vnc.open_tunnel([sys.executable, "-c", fail], dict(os.environ), port, timeout=20)


def test_open_tunnel_times_out():
    port = vnc.free_port()
    now = iter(range(0, 1000, 5))
    with pytest.raises(TransferError, match="no forward after 20 s"):
        vnc.open_tunnel([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), port, timeout=20,
                        sleep=lambda s: None, clock=lambda: next(now))


def test_open_tunnel_refuses_a_busy_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(TransferError, match="already in use"):
            vnc.open_tunnel([sys.executable, "-c", LISTEN, str(port)], dict(os.environ), port)


def test_session_desktop_tunnel(api, make_client, monkeypatch, capsys):
    api.on("POST", "/mac/sessions/ses_1/access",
           {"session_id": "ses_1", "ssh": {"host_key": "ssh-ed25519 HOSTKEY"}, "gateway": "https://gw", "token": "tok"})
    seen = {}

    def fake_ssh_argv(session_id, identity, command=None, *, forward=None):
        seen["forward"] = forward
        return [sys.executable, "-c", LISTEN, str(forward[0])]

    monkeypatch.setattr("qem.ssh.ssh_argv", fake_ssh_argv)
    with make_client().session("ses_1").desktop_tunnel() as tunnel:
        assert seen["forward"] == (tunnel.port, 5900)
        assert vnc.listening(tunnel.port)
    assert capsys.readouterr() == ("", "")
