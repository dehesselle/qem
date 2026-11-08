import sys
from pathlib import Path

import pytest

from qem import TransferError, ssh
from qem.cli import commands


def touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return str(path)


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """A fake Windows: its own OpenSSH, Git for Windows, and a settable PATH and session."""
    monkeypatch.delenv("QEM_SSH", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    machine = {
        "windows_ssh": touch(tmp_path / "Windows" / "System32" / "OpenSSH" / "ssh.exe"),
        "git": touch(tmp_path / "Git" / "cmd" / "git.exe"),
        "git_ssh": touch(tmp_path / "Git" / "usr" / "bin" / "ssh.exe"),
        "session": 0,
    }
    path = {"ssh": machine["windows_ssh"], "git": machine["git"]}
    machine["path"] = path
    monkeypatch.setattr(ssh.shutil, "which", lambda name: path.get(name))
    monkeypatch.setattr(ssh, "windows_session_id", lambda: machine["session"])
    return machine


def test_qem_ssh_wins(windows, monkeypatch):
    windows["path"]["myssh"] = "/opt/ssh/bin/ssh"
    monkeypatch.setenv("QEM_SSH", "myssh")
    assert ssh.choose_ssh() == ssh.SshClient("/opt/ssh/bin/ssh", "QEM_SSH")


def test_qem_ssh_that_does_not_exist_is_an_error(windows, monkeypatch):
    monkeypatch.setenv("QEM_SSH", "nossh")
    with pytest.raises(TransferError, match="QEM_SSH"):
        ssh.choose_ssh()


def test_without_a_desktop_gits_ssh_replaces_windows_openssh(windows):
    assert ssh.choose_ssh() == ssh.SshClient(windows["git_ssh"], "git")
    assert ssh.find_ssh() == windows["git_ssh"]


def test_on_a_desktop_the_ssh_on_path_stays(windows):
    windows["session"] = 1
    assert ssh.choose_ssh() == ssh.SshClient(windows["windows_ssh"], "PATH")


def test_unknown_session_changes_nothing(windows):
    windows["session"] = None
    assert ssh.choose_ssh().source == "PATH"


def test_another_ssh_on_path_is_respected(windows, tmp_path):
    windows["path"]["ssh"] = touch(tmp_path / "OpenSSH-Win64" / "ssh.exe")
    assert ssh.choose_ssh() == ssh.SshClient(windows["path"]["ssh"], "PATH")


def test_git_not_on_path_is_not_searched(windows):
    del windows["path"]["git"]
    assert ssh.choose_ssh() == ssh.SshClient(windows["windows_ssh"], "PATH")


def test_git_without_its_ssh(windows):
    Path(windows["git_ssh"]).unlink()
    assert ssh.choose_ssh().source == "PATH"


def test_git_in_mingw64_bin(windows, tmp_path):
    windows["path"]["git"] = touch(tmp_path / "Git" / "mingw64" / "bin" / "git.exe")
    assert ssh.choose_ssh() == ssh.SshClient(windows["git_ssh"], "git")


def test_no_ssh_on_path_but_git_without_a_desktop(windows):
    del windows["path"]["ssh"]
    assert ssh.choose_ssh().source == "git"
    windows["session"] = 1
    with pytest.raises(TransferError, match="not found"):
        ssh.choose_ssh()


def test_other_platforms_ignore_the_session(monkeypatch):
    monkeypatch.delenv("QEM_SSH", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ssh.shutil, "which", lambda name: {"ssh": "/usr/bin/ssh", "git": "/usr/bin/git"}.get(name))
    monkeypatch.setattr(ssh, "windows_session_id", lambda: pytest.fail("asked for a Windows session"))
    assert ssh.choose_ssh() == ssh.SshClient("/usr/bin/ssh", "PATH")


def test_msys_ssh_gets_a_posix_shell_for_the_proxycommand(windows):
    sh = touch(Path(windows["git_ssh"]).parent / "sh.exe")
    assert ssh.ssh_env() == {"SHELL": Path(sh).as_posix()}


def test_windows_openssh_needs_no_shell(windows):
    windows["session"] = 1
    assert ssh.ssh_env() == {}


def test_ssh_env_is_empty_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ssh.ssh_env() == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_windows_session_id_is_a_number():
    assert isinstance(ssh.windows_session_id(), int)


def test_cli_says_when_it_uses_gits_ssh(windows, capsys):
    commands.note_ssh_choice()
    assert "Git's ssh" in capsys.readouterr().err
    windows["session"] = 1
    commands.note_ssh_choice()
    assert capsys.readouterr().err == ""
