import io
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from qem import TransferError, ssh, transfer


@pytest.fixture(autouse=True)
def fixed_tools(monkeypatch):
    monkeypatch.setattr("qem.ssh.find_ssh", lambda: "/usr/bin/ssh")


def option(argv: list[str], name: str) -> str:
    values = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-o" and argv[i + 1].startswith(name + "=")]
    assert len(values) == 1, name
    return values[0].split("=", 1)[1]


def test_ssh_argv_pins_host_and_uses_tunnel(isolated_home):
    argv = ssh.ssh_argv("ses_1", Path("/k/id"), ["ls", "-la", "my dir"])
    assert argv[0] == "/usr/bin/ssh"
    assert argv[1:3] == ["-F", ssh._path(isolated_home / "empty")]
    assert option(argv, "HostKeyAlias") == "qem-ses_1"
    assert option(argv, "StrictHostKeyChecking") == "yes"
    assert option(argv, "GlobalKnownHostsFile") == ssh._path(isolated_home / "empty")
    assert option(argv, "ProxyCommand").endswith(" -m qem proxy ses_1")
    assert option(argv, "BatchMode") == "yes"
    assert option(argv, "ClearAllForwardings") == "yes"
    assert option(argv, "ForwardAgent") == "no"
    assert option(argv, "UserKnownHostsFile").endswith("known_hosts/ses_1")
    assert int(option(argv, "ServerAliveInterval")) * int(option(argv, "ServerAliveCountMax")) >= 60
    assert argv[-2:] == ["agent@qem-ses_1", "ls -la 'my dir'"]
    assert argv[argv.index("-i") + 1] == ssh._path(Path("/k/id"))
    assert option(argv, "IdentitiesOnly") == "yes"


def test_a_string_command_is_passed_as_is(isolated_home):
    assert ssh.ssh_argv("ses_1", None, "echo $HOME | wc")[-1] == "echo $HOME | wc"
    assert "-i" not in ssh.ssh_argv("ses_1", None, "true")


def test_interactive_and_forward_are_not_batch(isolated_home):
    assert not any(a.startswith("BatchMode") for a in ssh.ssh_argv("ses_1", None))
    argv = ssh.ssh_argv("ses_1", None, forward=(8080, 3000))
    assert argv[-4:] == ["-N", "-L", "127.0.0.1:8080:127.0.0.1:3000", "agent@qem-ses_1"]
    assert option(argv, "ExitOnForwardFailure") == "yes"
    assert not any(a.startswith(("ClearAllForwardings", "BatchMode")) for a in argv)


def test_forward_runs_no_command(isolated_home):
    with pytest.raises(TransferError):
        ssh.ssh_argv("ses_1", None, "ls", forward=(8080, 3000))


@pytest.mark.parametrize("session_id", ["", "../x", "a b", "-oProxyCommand=x", "ses;rm"])
def test_unusable_session_ids_are_refused(session_id):
    with pytest.raises(TransferError):
        ssh.host_alias(session_id)


@pytest.fixture
def short_home(monkeypatch):
    """A home short enough for a socket path (pytest's tmp_path is too long)."""
    home = Path(tempfile.mkdtemp(prefix="qem"))
    monkeypatch.setenv("QEM_HOME", str(home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


def test_multiplexing_off_windows(short_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    argv = ssh.ssh_argv("ses_1", None, "true")
    assert option(argv, "ControlMaster") == "auto"
    path = Path(option(argv, "ControlPath"))
    assert path.parent == short_home / "mux" and len(path.name) == 16
    if os.name == "posix":
        assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert option(argv, "ControlPersist").isdigit()
    other = Path(option(ssh.ssh_argv("ses_2", None, "true"), "ControlPath"))
    assert other != path  # one shared connection per Mac

    # a forward lives and dies with its own ssh
    assert not any(a.startswith("Control") for a in ssh.ssh_argv("ses_1", None, forward=(1, 2)))


def test_no_multiplexing_on_windows_or_with_a_long_path(short_home, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert not any(a.startswith("Control") for a in ssh.ssh_argv("ses_1", None, "true"))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("QEM_HOME", str(tmp_path / ("x" * 100)))
    assert not any(a.startswith("Control") for a in ssh.ssh_argv("ses_1", None, "true"))


def test_paths_with_spaces_are_quoted(monkeypatch, tmp_path):
    monkeypatch.setenv("QEM_HOME", str(tmp_path / "my home"))
    argv = ssh.ssh_argv("ses_1", None, "true")
    assert option(argv, "UserKnownHostsFile") == f'"{ssh._path(tmp_path / "my home" / "known_hosts" / "ses_1")}"'


def test_proxy_command_escapes_percent(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/100%/python")
    assert "/opt/100%%/python" in ssh.proxy_command("ses_1")


def test_ssh_config_block(isolated_home):
    block = ssh.ssh_config_block("nomac", "ses_1", Path("/k/id"))
    assert block.startswith("Host nomac\n") and block.endswith("\n")
    lines = block.splitlines()
    assert all(line.startswith("    ") for line in lines[1:])
    for line in ("HostKeyAlias qem-ses_1", "User agent", "IdentitiesOnly yes", "StrictHostKeyChecking yes",
                 f"IdentityFile {ssh._path(Path('/k/id'))}",
                 f"UserKnownHostsFile {ssh._path(isolated_home / 'known_hosts' / 'ses_1')}"):
        assert f"    {line}" in lines, line
    assert any(line.startswith("    ProxyCommand ") and line.endswith(" -m qem proxy ses_1") for line in lines)
    assert any(line.startswith("    ServerAliveInterval ") for line in lines)
    assert "IdentityFile" not in ssh.ssh_config_block("nomac", "ses_1", None)


needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")


@needs_keygen
def test_generate_identity(isolated_home):
    key = isolated_home / "keys" / "id_test"
    assert ssh.generate_identity(key) == key
    public = Path(f"{key}.pub").read_text()
    assert public.startswith("ssh-ed25519 ") and public.split()[-1] == "qem"
    assert "PRIVATE KEY" in key.read_text()
    if os.name == "posix":
        assert (key.stat().st_mode & 0o777) == 0o600
        assert (key.parent.stat().st_mode & 0o777) == 0o700
    with pytest.raises(TransferError, match="already exists"):
        ssh.generate_identity(key)


def test_generate_identity_without_keygen(isolated_home, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(TransferError, match="ssh-keygen not found"):
        ssh.generate_identity(isolated_home / "keys" / "id_test")


@pytest.mark.skipif(os.name != "posix", reason="uses a shell script as ssh-keygen")
def test_generate_identity_reports_keygen_errors(isolated_home, monkeypatch, tmp_path):
    keygen = tmp_path / "ssh-keygen"
    keygen.write_text("#!/bin/sh\necho 'no entropy' >&2\nexit 3\n")
    keygen.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: str(keygen))
    with pytest.raises(TransferError, match=r"exit code 3\): no entropy"):
        ssh.generate_identity(isolated_home / "keys" / "id_test")


@pytest.mark.parametrize("name", ["My App", ".hidden", "Über's \"app\"", "x" * 120])
def test_remote_name_is_the_folder_name(tmp_path, name):
    if os.name == "nt" and '"' in name:
        name = name.replace('"', "")  # not allowed in Windows file names
    folder = tmp_path / name
    folder.mkdir()
    assert transfer.remote_name(folder) == name


def test_remote_name_is_nfc(tmp_path):
    folder = tmp_path / "Über"  # Ü as U plus a combining mark
    folder.mkdir()
    assert transfer.remote_name(folder) == "Über"


def test_remote_name_refuses_what_macos_cannot_hold(tmp_path):
    with pytest.raises(TransferError, match="drive root"):
        transfer.remote_name(Path(tmp_path.anchor))
    with pytest.raises(TransferError, match="too long"):
        transfer.remote_name(Path("ü" * 128))  # 256 bytes in UTF-8


def test_remote_path_with_odd_characters_stays_one_shell_word():
    remote = transfer.resolve_remote("build/App.ipa", "Über's \"app\" $(x)")
    command = transfer.pull_command(remote)
    assert shlex.split(command)[2] == "/Users/agent/drive/src/Über's \"app\" $(x)/build"


def test_resolve_remote():
    assert transfer.resolve_remote("build", "App") == "/Users/agent/drive/src/App/build"
    assert transfer.resolve_remote("/tmp/x", "App") == "/tmp/x"
    assert transfer.resolve_remote("~/Library", "App") == "~/Library"


@pytest.mark.parametrize(
    "remote, command",
    [
        ("/Users/agent/drive/src/App/build/", "cd -- /Users/agent/drive/src/App && COPYFILE_DISABLE=1 exec tar -cf - -- build"),
        ("~/Library/My Logs", "cd -- \"$HOME\"/Library && COPYFILE_DISABLE=1 exec tar -cf - -- 'My Logs'"),
        ("~/x.txt", "cd -- \"$HOME\" && COPYFILE_DISABLE=1 exec tar -cf - -- x.txt"),
    ],
)
def test_pull_command(remote, command):
    assert transfer.pull_command(remote) == command


def make_tar(entries: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_extract_keeps_or_overwrites(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_bytes(b"local")
    entries = {"out/a.txt": b"remote", "out/sub/b.bin": b"\x00\xff\r\n"}

    assert transfer.extract(make_tar(entries), tmp_path, overwrite=False) == (1, 1)
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"local"
    assert (tmp_path / "out" / "sub" / "b.bin").read_bytes() == b"\x00\xff\r\n"

    assert transfer.extract(make_tar(entries), tmp_path, overwrite=True) == (2, 0)
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"remote"


def test_extract_refuses_paths_outside_dest(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(tarfile.TarError):
        transfer.extract(make_tar({"../evil.txt": b"x"}), dest, overwrite=True)
    assert not (tmp_path / "evil.txt").exists()
