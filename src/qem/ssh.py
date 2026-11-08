# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""ssh command lines for a session.

ssh never resolves the Mac's name: ProxyCommand runs `python -m qem
proxy <id>`, which carries the SSH bytes over a WebSocket. The host key comes
from the access call and is pinned in a per-session known_hosts file.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config
from .errors import TransferError

USERNAME = "agent"

# A tunnel can stall while the provider's network blips. 15 s keepalives with
# 8 misses give it two minutes before ssh declares the connection dead.
KEEPALIVE_INTERVAL = 15
KEEPALIVE_MISSES = 8
# With a ProxyCommand this bounds the wait for the Mac's SSH banner. The tunnel
# itself gives up on the WebSocket handshake after 15 s.
CONNECT_TIMEOUT = 30
# A shared connection stays up this long after its last use. Kept short,
# because an open connection may keep a billed Mac from counting as idle.
CONTROL_PERSIST = 60
# sun_path holds 104 bytes on macOS (108 on Linux), and ssh first binds a
# temporary name: the ControlPath plus 17 characters.
MAX_CONTROL_PATH = 104 - 1 - 17

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _path(p: Path | str) -> str:
    # Forward slashes work for Windows OpenSSH and for MSYS ssh, whose /bin/sh
    # would eat the backslashes in a ProxyCommand.
    return Path(p).as_posix() if sys.platform == "win32" else str(p)


def _value(text: str) -> str:
    """Quote an ssh option value that contains spaces."""
    return f'"{text}"' if " " in text else text


def host_alias(session_id: str) -> str:
    """The local-only name ssh uses for a session's Mac: "qem-<session id>".

    It is the HostKeyAlias and the host part of the ssh target. The NoMac server
    never sees it; the ProxyCommand ignores it.
    """
    # The id ends up in a host name, file names and the ProxyCommand's shell line.
    if not _SESSION_ID.fullmatch(session_id):
        raise TransferError(f"not a usable session id: {session_id!r}")
    return f"qem-{session_id}"


def target(session_id: str) -> str:
    """The ssh destination: user `USERNAME` at `host_alias(session_id)`."""
    return f"{USERNAME}@{host_alias(session_id)}"


@dataclass(frozen=True)
class SshClient:
    """The ssh executable qem runs, and why: "QEM_SSH", "git" or "PATH"."""

    path: str
    source: str


def windows_session_id() -> int | None:
    """This process's Windows session, or None if Windows does not say.

    Session 0 is where services and ssh logins run, without a desktop.
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    session = ctypes.c_ulong()
    if kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(), ctypes.byref(session)
    ):
        return session.value
    return None


def _is_windows_openssh(exe: str) -> bool:
    """Whether `exe` is the OpenSSH that ships with Windows."""
    root = os.environ.get("SystemRoot")
    if not root:
        return False
    bundled = Path(root) / "System32" / "OpenSSH"
    return os.path.normcase(str(Path(exe).parent)) == os.path.normcase(str(bundled))


def _gits_ssh() -> str | None:
    """ssh.exe of the Git for Windows whose `git` is on PATH.

    Git for Windows keeps git.exe in <root>\\cmd, <root>\\bin or
    <root>\\mingw64\\bin, and ssh.exe in <root>\\usr\\bin. Nothing else is searched.
    """
    git = shutil.which("git")
    if not git:
        return None
    root = Path(git).parent.parent
    if root.name.lower() == "mingw64":
        root = root.parent
    candidate = root / "usr" / "bin" / "ssh.exe"
    return str(candidate) if candidate.is_file() else None


def choose_ssh() -> SshClient:
    """Pick the ssh executable.

    1. QEM_SSH, if set (a path, or a name looked up on PATH).
    2. On Windows without a desktop (session 0), when the ssh on PATH is
       Windows' own OpenSSH: Git's ssh, if Git is on PATH. Windows OpenSSH
       stops writing its output there, so commands would never finish.
    3. ssh on PATH.
    """
    if wanted := os.environ.get("QEM_SSH"):
        if found := shutil.which(wanted):
            return SshClient(found, "QEM_SSH")
        raise TransferError(f"QEM_SSH is {wanted!r}, but there is no such program")
    exe = shutil.which("ssh")
    if (
        sys.platform == "win32"
        and (exe is None or _is_windows_openssh(exe))
        and windows_session_id() == 0
    ):
        if git_ssh := _gits_ssh():
            return SshClient(git_ssh, "git")
    if not exe:
        raise TransferError("ssh not found on PATH")
    return SshClient(exe, "PATH")


def find_ssh() -> str:
    return choose_ssh().path


def ssh_env() -> dict[str, str]:
    """Environment variables the chosen ssh needs on top of os.environ.

    MSYS ssh (Git's) runs the ProxyCommand through $SHELL. In an ssh login to a
    Windows computer, SHELL is that server's shell, often PowerShell, which
    cannot run it. So point SHELL at the sh.exe that sits next to such an ssh.
    """
    if sys.platform != "win32":
        return {}
    try:
        exe = Path(choose_ssh().path)
    except TransferError:
        return {}
    sh = exe.parent / "sh.exe"
    return {"SHELL": sh.as_posix()} if sh.is_file() else {}


def generate_identity(path: Path) -> Path:
    """Create a new ed25519 key pair at `path` and `path`.pub with ssh-keygen.

    No passphrase, no prompts, comment "qem". Create the parent folder privately.
    On Windows, restrict the private key with `restrict_to_owner`. Raise
    TransferError when ssh-keygen is missing or fails (with its error text).
    Return `path`.
    """
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise TransferError("ssh-keygen not found on PATH")
    if path.exists():
        # ssh-keygen would stop and ask whether to overwrite it.
        raise TransferError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "qem", "-f", str(path)]
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise TransferError(f"ssh-keygen could not run: {e}") from e
    if result.returncode != 0 or not path.exists():
        detail = (result.stderr or result.stdout).strip()
        raise TransferError(
            f"ssh-keygen failed (exit code {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    if sys.platform == "win32":
        restrict_to_owner(path)
    return path


def _current_user_sid() -> str:
    """The Windows SID of the user running qem, from `whoami /user`."""
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    fields = next(csv.reader([result.stdout.strip()]), [])
    if result.returncode != 0 or len(fields) < 2 or not fields[1].startswith("S-"):
        raise TransferError(
            f"cannot find the current user's SID: {(result.stderr or result.stdout).strip()}"
        )
    return fields[1]


def restrict_to_owner(path: Path) -> None:
    """Leave only the current user in the file's ACL.

    Windows OpenSSH ignores a private key that anyone else may read, and MSYS
    ssh-keygen (Git Bash) adds an OWNER RIGHTS entry that counts as such.
    """
    # By SID: in an ssh session USERDOMAIN can be WORKGROUP, a name icacls cannot resolve.
    for args in (
        ["/reset"],
        ["/inheritance:r", "/grant:r", f"*{_current_user_sid()}:F"],
    ):
        result = subprocess.run(
            ["icacls", str(path), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise TransferError(
                f"icacls {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
            )


def known_hosts_path(session_id: str) -> Path:
    return config.home() / "known_hosts" / session_id


def write_known_hosts(session_id: str, host_key: str) -> Path:
    path = known_hosts_path(session_id)
    config.write_private(path, f"{host_alias(session_id)} {host_key}\n")
    return path


def keys_dir() -> Path:
    return config.home() / "keys"


def remove_identity(identity: Path | None) -> None:
    """Delete a key pair, but only one qem generated; a key the caller passed in stays."""
    if identity and Path(identity).parent == keys_dir():
        for path in (Path(identity), Path(f"{identity}.pub")):
            path.unlink(missing_ok=True)


def forget(session_id: str, identity: Path | None) -> None:
    """A stopped Mac never accepts its key again; remove the files that belonged to it."""
    known_hosts_path(session_id).unlink(missing_ok=True)
    remove_identity(identity)


def empty_file() -> Path:
    """Stands in for /dev/null, which Windows OpenSSH does not understand."""
    path = config.home() / "empty"
    if not path.exists():
        config.write_private(path, "")
    return path


def proxy_command(session_id: str) -> str:
    python = _path(sys.executable)
    if sys.platform == "win32":
        python = f'"{python}"'
    else:
        python = shlex.quote(python)
    # ssh expands %-tokens in ProxyCommand.
    return f"{python} -m qem proxy {session_id}".replace("%", "%%")


def _multiplex_options(session_id: str) -> list[str]:
    """ssh options that let repeated ssh calls to one session share a connection.

    Requirements: none on Windows (Windows OpenSSH cannot multiplex). The control
    socket lives under `config.home()` in a private folder and is specific to the
    session. Unix socket paths are short (macOS allows about 104 bytes), so when
    the path would be too long, return no options rather than fail. The shared
    master should go away by itself some time after the last use.
    """
    if sys.platform == "win32":
        return []
    folder = config.home() / "mux"
    # A fixed-length name keeps the path short whatever the id looks like.
    path = _path(folder / hashlib.sha256(session_id.encode()).hexdigest()[:16])
    if len(os.fsencode(path)) > MAX_CONTROL_PATH:
        return []
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    except OSError:
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_value(path.replace('%', '%%'))}",  # ssh expands %-tokens here
        "-o", f"ControlPersist={CONTROL_PERSIST}",
    ]  # fmt: skip


def _settings(session_id: str) -> list[tuple[str, str]]:
    """What ssh_options and ssh_config_block have in common, as (keyword, value)."""
    return [
        ("HostKeyAlias", host_alias(session_id)),
        ("UserKnownHostsFile", _value(_path(known_hosts_path(session_id)))),
        ("GlobalKnownHostsFile", _value(_path(empty_file()))),
        ("StrictHostKeyChecking", "yes"),
        ("UpdateHostKeys", "no"),  # the pin stays exactly what /access returned
        ("CheckHostIP", "no"),
        # The rest of the line is the command; proxy_command quotes it itself.
        ("ProxyCommand", proxy_command(session_id)),
        ("ForwardAgent", "no"),
        ("ForwardX11", "no"),
        ("ServerAliveInterval", str(KEEPALIVE_INTERVAL)),
        ("ServerAliveCountMax", str(KEEPALIVE_MISSES)),
        ("ConnectTimeout", str(CONNECT_TIMEOUT)),
    ]


def ssh_options(
    session_id: str,
    identity: Path | None,
    *,
    batch: bool = True,
    multiplex: bool = True,
) -> list[str]:
    """ssh options (without the ssh executable) that reach the Mac through the tunnel.

    Requirements:
    - The user's own ssh config must not apply (use `empty_file()`, since Windows
      OpenSSH does not understand /dev/null).
    - The Mac's host key is pinned: only `known_hosts_path(session_id)` counts,
      under the name `host_alias(session_id)`, no global known_hosts, and an
      unknown or changed key is refused, never accepted.
    - The connection is made by ProxyCommand `proxy_command(session_id)`.
    - No agent forwarding.
    - A dead tunnel is noticed (keepalives) and a connect attempt does not hang
      forever. The tunnel can pause for a while when the provider's network
      blips, so do not give up too eagerly.
    - With `identity`, use only that key.
    - Multiplexing from `_multiplex_options`.
    - With `batch` (non-interactive commands): never prompt, and no port
      forwarding.
    Paths go through `_path` and `_value` (Windows and MSYS ssh both parse them).
    `multiplex=False` gives this ssh a connection of its own.
    """
    options = ["-F", _path(empty_file())]
    for keyword, value in _settings(session_id):
        options += ["-o", f"{keyword}={value}"]
    # Keeps "Connection to ... closed." and the like off stderr; errors still show.
    options += ["-o", "LogLevel=ERROR"]
    if identity:
        options += ["-i", _path(identity), "-o", "IdentitiesOnly=yes"]
    if batch:
        options += ["-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes"]
    if multiplex:
        options += _multiplex_options(session_id)
    return options


def ssh_argv(
    session_id: str,
    identity: Path | None,
    command: str | Sequence[str] | None = None,
    *,
    forward: tuple[int, int] | None = None,
) -> list[str]:
    """Full argv for ssh: the ssh executable (`find_ssh`), `ssh_options`, the target.

    - `command` None: an interactive login. A list is quoted for the Mac's shell
      (POSIX); a string is passed as is.
    - `forward=(local, remote)`: forward 127.0.0.1:local on this computer to
      127.0.0.1:remote on the Mac and run no command. If the local port cannot be
      bound, ssh must fail at once instead of staying up without the forward.
    - Batch mode only for a command without a forward.
    """
    if forward is not None and command is not None:
        raise TransferError("a port forward runs no command")
    # A forward gets a connection of its own: in a shared master the port would
    # stay open after this ssh ends, and a busy port would not fail this ssh.
    options = ssh_options(
        session_id, identity, batch=command is not None, multiplex=forward is None
    )
    argv = [find_ssh(), *options]
    if forward is not None:
        local, remote = forward
        argv += [
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            f"127.0.0.1:{local}:127.0.0.1:{remote}",
        ]
    argv.append(target(session_id))
    if command is not None:
        argv.append(command if isinstance(command, str) else shlex.join(command))
    return argv


def ssh_config_block(name: str, session_id: str, identity: Path | None) -> str:
    """A `Host <name>` block for the user's ssh config, so plain `ssh <name>`,
    scp, rsync and editors reach the Mac the same way `ssh_options` does
    (pinned host key, ProxyCommand, user, keepalives, identity). Newline-terminated.
    """
    lines = [f"Host {name}", f"    User {USERNAME}"]
    lines += [f"    {keyword} {value}" for keyword, value in _settings(session_id)]
    if identity:
        lines += [
            f"    IdentityFile {_value(_path(identity))}",
            "    IdentitiesOnly yes",
        ]
    return "\n".join(lines) + "\n"
