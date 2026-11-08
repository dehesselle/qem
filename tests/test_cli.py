# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import subprocess
import sys

import httpx
import pytest

from qem import Nomac, NomacError, TransferError
from qem.cli import EXIT_STILL_RUNNING, main
from qem.cli.commands import copy_to_clipboard, parse_duration, parse_forward
from qem.cli.project_store import ProjectStore, find_identity, sessions_root


@pytest.fixture
def cli(api, monkeypatch, tmp_path):
    """Run `qem ARGS` in a project folder against the fake API; returns the exit code."""
    monkeypatch.setattr(
        "qem.cli.commands.Nomac",
        lambda **kw: Nomac(
            http_client=httpx.Client(transport=httpx.MockTransport(api)),
            sleep=lambda s: None,
            **kw,
        ),
    )
    project = tmp_path / "MyApp"
    project.mkdir()
    monkeypatch.chdir(project)

    def run(*args: str) -> int:
        with pytest.raises(SystemExit) as exc:
            main(list(args))
        return exc.value.code

    return run


def attach(session_id="ses_1"):
    ProjectStore().save({"id": session_id, "identity": None})


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("qem ")


@pytest.mark.parametrize(
    "text, seconds",
    [("90s", 90), ("45m", 2700), ("2h", 7200), ("10", 600), ("1.5h", 5400)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_exec_prints_and_returns_exit_code(cli, api, capsys):
    attach()
    api.on(
        "POST",
        "/mac/sessions/ses_1/jobs",
        {
            "id": "job_1",
            "state": "exited",
            "exit_code": 2,
            "stdout": "out\n",
            "stderr": "err\n",
        },
    )
    assert cli("exec", "--", "false") == 2
    out = capsys.readouterr()
    assert out.out == "out\n" and out.err == "err\n"
    assert api.body(0) == {"argv": ["false"]}


def test_exec_still_running_exits_75(cli, api, capsys):
    attach()
    api.on("POST", "/mac/sessions/ses_1/jobs", {"id": "job_1", "state": "running"})
    assert cli("exec", "--wait", "5", "--", "sleep", "600") == EXIT_STILL_RUNNING
    out = capsys.readouterr()
    assert out.out == ""
    assert "after 5 s" in out.err
    assert "qem job job_1 --wait 600" in out.err and "qem cancel job_1" in out.err


def test_exec_unknown_state_exits_1(cli, api):
    attach()
    api.on("POST", "/mac/sessions/ses_1/jobs", {"id": "job_1", "state": "unknown"})
    assert cli("exec", "--", "true") == 1


def test_exec_reads_truncated_output(cli, api, capsys):
    attach()
    api.on(
        "POST",
        "/mac/sessions/ses_1/jobs",
        {
            "id": "job_1",
            "state": "exited",
            "exit_code": 0,
            "stdout": "first ",
            "stderr": "",
            "stdout_next_cursor": 6,
        },
    )
    api.on(
        "GET",
        "/mac/sessions/ses_1/jobs/job_1/output",
        {"data_base64": "c2Vjb25k", "next_cursor": 12, "eof": True},
    )
    assert cli("exec", "--", "cat", "big") == 0
    assert capsys.readouterr().out == "first second"


def test_exec_uses_synced_folder(cli, api):
    ProjectStore().save({"id": "ses_1", "synced_dir": "/Users/agent/drive/src/MyApp"})
    api.on(
        "POST",
        "/mac/sessions/ses_1/jobs",
        {"id": "job_1", "state": "exited", "exit_code": 0, "stdout": "", "stderr": ""},
    )
    cli("exec", "--", "ls")
    assert api.body(0)["cwd"] == "/Users/agent/drive/src/MyApp"


def test_no_session_for_folder(cli, capsys):
    assert cli("status") == 1
    assert "qem start" in capsys.readouterr().err


def test_payment_required_shows_buy_url(cli, api, capsys):
    api.on(
        "GET",
        "/mac/credits",
        (
            402,
            {
                "error": {
                    "code": "insufficient_credit",
                    "message": "No credit",
                    "buy_url": "https://nomac.app/buy",
                }
            },
        ),
    )
    assert cli("credits") == 1
    assert "https://nomac.app/buy" in capsys.readouterr().err


def test_start_refuses_while_another_mac_runs(cli, api, monkeypatch, capsys):
    monkeypatch.setattr(
        "qem.nomac.generate_identity",
        lambda path: (
            (
                path.parent.mkdir(parents=True),
                path.write_text("k"),
                path.with_name(path.name + ".pub").write_text("ssh-ed25519 K"),
            )[0]
            or path
        ),
    )
    api.on(
        "POST",
        "/mac/sessions",
        (409, {"error": {"code": "session_limit_reached", "message": "A Mac runs"}}),
    )
    api.on(
        "GET",
        "/mac/sessions",
        {
            "sessions": [
                {"id": "ses_old", "state": "stopped"},
                {"id": "ses_run", "state": "ready"},
            ]
        },
    )
    assert cli("start") == 1
    err = capsys.readouterr().err
    assert "ses_run" in err and "qem attach ses_run" in err and "--parallel" in err
    assert "ses_old" not in err
    assert ProjectStore().load() is None


def test_ssh_config_takes_the_host_name(cli, api, monkeypatch, isolated_home, tmp_path):
    ProjectStore().save({"id": "ses_1", "identity": str(tmp_path / "id_test")})
    monkeypatch.setattr("qem.ssh.ssh_env", dict)
    monkeypatch.setattr(
        "qem.cli.commands.ensure_include", lambda include, assume_yes: False
    )
    api.on(
        "POST",
        "/mac/sessions/ses_1/access",
        {
            "session_id": "ses_1",
            "generation": 1,
            "ssh": {"username": "agent", "host_key": "ssh-ed25519 HOSTKEY"},
            "gateway": "https://gw.nomac.app",
            "token": "t" * 40,
            "expires_at": "2026-10-30T13:00:00Z",
        },
    )
    assert cli("ssh-config", "mac2") == 0
    lines = (isolated_home / "ssh_config.d" / "mac2.conf").read_text().splitlines()
    assert lines[0] == "Host mac2"
    assert any(
        line.strip().startswith("ProxyCommand ")
        and line.endswith(" -m qem proxy ses_1")
        for line in lines
    )


def test_ssh_config_quotes_an_include_with_a_space(cli, api, monkeypatch, tmp_path):
    # as in macOS's ~/Library/Application Support/qem
    home = tmp_path / "Application Support" / "qem"
    monkeypatch.setenv("QEM_HOME", str(home))
    ProjectStore().save({"id": "ses_1", "identity": str(tmp_path / "id_test")})
    monkeypatch.setattr("qem.ssh.ssh_env", dict)
    includes = []
    monkeypatch.setattr(
        "qem.cli.commands.ensure_include",
        lambda include, assume_yes: includes.append(include) or False,
    )
    api.on(
        "POST",
        "/mac/sessions/ses_1/access",
        {
            "session_id": "ses_1",
            "generation": 1,
            "ssh": {"username": "agent", "host_key": "ssh-ed25519 HOSTKEY"},
            "gateway": "https://gw.nomac.app",
            "token": "t" * 40,
            "expires_at": "2026-10-30T13:00:00Z",
        },
    )
    assert cli("ssh-config", "mac2") == 0
    assert includes == [f'Include "{(home / "ssh_config.d" / "*.conf").as_posix()}"']


def test_ssh_config_refuses_odd_host_names(cli):
    attach()
    assert cli("ssh-config", "-x") != 0  # argparse takes it for an option
    assert cli("ssh-config", "a b") == 1


def test_stop_clears_store(cli, api):
    attach()
    api.on(
        "DELETE",
        "/mac/sessions/ses_1",
        {"session": {"id": "ses_1", "state": "stopped"}},
    )
    assert cli("stop") == 0
    assert ProjectStore().load() is None


PASSWORD = {
    "session_id": "ses_1",
    "user": "agent",
    "password": "s3cret",
    "variable": "NOMAC_MAC_PASSWORD",
}


def test_password_prints_it(cli, api, capsys):
    attach()
    api.on("GET", "/mac/sessions/ses_1/password", PASSWORD)
    assert cli("password") == 0
    out = capsys.readouterr()
    assert out.out == "s3cret\n" and "agent" in out.err


def test_password_copy(cli, api, capsys, monkeypatch):
    attach()
    api.on("GET", "/mac/sessions/ses_1/password", PASSWORD)
    copied = []
    monkeypatch.setattr("qem.cli.commands.copy_to_clipboard", copied.append)
    assert cli("password", "--copy") == 0
    assert copied == ["s3cret"] and "s3cret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "text, ports", [("8080:3000", (8080, 3000)), (" 1:65535 ", (1, 65535))]
)
def test_parse_forward(text, ports):
    assert parse_forward(text) == ports


@pytest.mark.parametrize(
    "text",
    ["8080", "0:80", "80:0", "70000:80", "a:b", "8080:3000:1", "-1:80", ":80", ""],
)
def test_parse_forward_refuses(text):
    with pytest.raises(NomacError, match="LOCAL:REMOTE"):
        parse_forward(text)


def test_ssh_forward(cli, monkeypatch, capsys):
    ProjectStore().save({"id": "ses_1", "identity": "/k/id"})
    calls = []
    monkeypatch.setattr(
        "qem.mac_session.MacSession.ssh",
        lambda self, command=None, *, forward=None: (
            calls.append((command, forward)) or subprocess.CompletedProcess([], 0)
        ),
    )
    assert cli("ssh", "--forward", "8080:3000") == 0
    assert calls == [(None, (8080, 3000))]
    assert "http://127.0.0.1:8080" in capsys.readouterr().err

    assert cli("ssh", "--forward", "8080", "--", "ls") == 1
    assert cli("ssh", "--forward", "8080:3000", "--", "ls") == 1
    assert "runs no command" in capsys.readouterr().err
    assert len(calls) == 1


# the project store


def test_store_lives_outside_the_project(tmp_path, isolated_home):
    project = tmp_path / "App"
    project.mkdir()
    store = ProjectStore(project)
    assert store.project == project.resolve()
    assert store.path.parent == sessions_root() and sessions_root().is_relative_to(
        isolated_home
    )
    assert store.load() is None

    store.save({"id": "ses_1", "identity": None})
    assert ProjectStore(project).load() == {
        "id": "ses_1",
        "identity": None,
        "project": str(project.resolve()),
    }
    assert ProjectStore(project / "." / ".." / "App").path == store.path
    assert ProjectStore(tmp_path).path != store.path
    assert list(project.iterdir()) == []

    assert store.update(synced_dir="/x")["synced_dir"] == "/x"
    assert store.load()["id"] == "ses_1"

    store.clear()
    assert store.load() is None and not sessions_root().exists()


def test_store_ignores_broken_records(tmp_path):
    store = ProjectStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    for text in ("not json", "[1]", '{"identity": "/k"}'):
        store.path.write_text(text)
        assert store.load() is None
    store.path.write_text('{"id": "ses_1", "project": "/somewhere/else"}')
    assert store.load() is None


def test_find_identity(tmp_path):
    key = tmp_path / "id_key"
    key.write_text("k")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ProjectStore(tmp_path / "a").save(
        {"id": "ses_1", "identity": str(tmp_path / "gone")}
    )
    ProjectStore(tmp_path / "b").save({"id": "ses_1", "identity": str(key)})
    assert find_identity("ses_1") == str(key)
    assert find_identity("ses_2") is None


# the clipboard


@pytest.fixture
def clipboard(monkeypatch):
    """Every tool exists; returns the runs and lets a test pick which tools fail."""
    runs, failing = [], set()
    monkeypatch.setattr("qem.cli.commands.shutil.which", lambda name: f"/bin/{name}")

    def run(argv, *, input, **kwargs):
        runs.append((argv, input))
        return subprocess.CompletedProcess(
            argv, 1 if argv[0].rsplit("/", 1)[-1] in failing else 0
        )

    monkeypatch.setattr("qem.cli.commands.subprocess.run", run)
    return runs, failing


def test_clipboard_on_windows_is_utf16(clipboard, monkeypatch):
    runs, _ = clipboard
    monkeypatch.setattr(sys, "platform", "win32")
    copy_to_clipboard("pässword")
    assert runs == [(["/bin/clip"], "pässword".encode("utf-16-le"))]
    assert not runs[0][1].startswith(b"\xff\xfe")


def test_clipboard_on_linux_tries_tools_in_turn(clipboard, monkeypatch):
    runs, failing = clipboard
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    failing.update({"wl-copy", "xclip"})
    copy_to_clipboard("pw")
    assert [argv[0] for argv, _ in runs] == ["/bin/wl-copy", "/bin/xclip", "/bin/xsel"]
    assert runs[-1] == (["/bin/xsel", "--clipboard", "--input"], b"pw")


def test_clipboard_without_a_tool(clipboard, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    clipboard[1].add("pbcopy")
    with pytest.raises(NomacError, match="no working clipboard tool"):
        copy_to_clipboard("pw")


class FakeTunnel:
    def __init__(self, port, wait):
        self.port, self._wait, self.closed = port, wait, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def wait(self):
        return self._wait()


@pytest.fixture
def desktop(cli, api, monkeypatch):
    """`qem desktop` with a fake tunnel on port 5999 and a fake window;
    returns (tunnels, windows), windows being show_desktop's arguments."""
    ProjectStore().save({"id": "ses_1", "identity": "/k/id"})
    api.on("GET", "/mac/sessions/ses_1/password", PASSWORD)
    tunnels, windows = [], []

    def interrupted():
        raise KeyboardInterrupt

    def tunnel(self, port=None):
        tunnels.append(FakeTunnel(port or 5999, interrupted))
        return tunnels[-1]

    monkeypatch.setattr("qem.mac_session.MacSession.desktop_tunnel", tunnel)
    monkeypatch.setattr(
        "qem.vnc.show_desktop",
        lambda tunnel, login, **kw: windows.append((tunnel, login, kw)),
    )
    return tunnels, windows


def test_desktop_opens_the_window_signed_in(cli, desktop, capsys):
    tunnels, windows = desktop
    assert cli("desktop") == 0
    tunnel, login, kw = windows[0]
    assert tunnel is tunnels[0] and tunnel.closed
    assert (login.user, login.password) == ("agent", "s3cret")
    assert kw["title"] == "NoMac ses_1"
    kw["reopen"]()  # a dropped ssh is replaced on the same port
    assert tunnels[1].port == 5999
    out = capsys.readouterr()
    assert out.out == "" and "s3cret" not in out.err


def test_desktop_without_a_window(cli, desktop, capsys, monkeypatch):
    def no_window(tunnel, login, **kw):
        raise RuntimeError("the window cannot be drawn to")

    monkeypatch.setattr("qem.vnc.show_desktop", no_window)
    assert cli("desktop") == 1
    assert "--no-viewer" in capsys.readouterr().err
    assert desktop[0][0].closed


def test_desktop_reports_a_failed_session(cli, desktop, capsys, monkeypatch):
    def failed(tunnel, login, **kw):
        raise TransferError("the desktop session ended: hung up")

    monkeypatch.setattr("qem.vnc.show_desktop", failed)
    assert cli("desktop") == 1
    assert "qem: the desktop session ended: hung up" in capsys.readouterr().err


def test_desktop_no_viewer_holds_until_ctrl_c(cli, desktop, capsys):
    tunnels, windows = desktop
    assert cli("desktop", "--no-viewer", "--port", "5999") == 0
    out = capsys.readouterr()
    assert out.out == "127.0.0.1::5999\n" and "s3cret" not in out.out + out.err
    assert not windows and tunnels[0].closed


def test_desktop_reports_a_closed_tunnel(cli, desktop, capsys, monkeypatch):
    monkeypatch.setattr(
        "qem.mac_session.MacSession.desktop_tunnel",
        lambda self, port=None: FakeTunnel(5999, lambda: 255),
    )
    assert cli("desktop", "--no-viewer") == 1
    assert "ssh exit code 255" in capsys.readouterr().err


def test_desktop_needs_the_ssh_key(cli, capsys):
    attach()
    assert cli("desktop") == 1
    assert "no SSH key" in capsys.readouterr().err
