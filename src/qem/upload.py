# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Project upload (`sync`): compare with the Mac, then send changed files as tar over ssh.

One ssh connection per sync runs `remote_script` on the Mac with macOS's own
tools. The script lists the project folder there, reads which paths to delete
from stdin and then unpacks a tar stream of new and changed files. Files are
compared by type, size, modification time and permission bits.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pathspec

from .errors import TransferError

DEFAULT_SKIPS = ("node_modules", "Pods", "DerivedData", ".build", "build", ".git")
# Secrets stay on this computer unless a `!` line in .nomacignore sends them.
# .gitignore syntax; the `!` lines spare the usual committed templates.
SECRETS = (
    ".env", ".env.*", "!.env.example", "!.env.sample", "!.env.template",
    "*.p8", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.ppk",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
)  # fmt: skip
# Files with these suffixes are secrets only when they hold a private key, so
# certificates and Keynote documents still go.
KEY_SUFFIXES = (".pem", ".key")
_KEY_MARK = b"PRIVATE KEY-----"
_KEY_READ = 64 * 1024  # a certificate chain can come before the key
NOMACIGNORE = ".nomacignore"
GITIGNORE = ".gitignore"
BEGIN = b"qem-manifest\n"
END = b"qem-manifest-end\n"
_GLOB = re.compile(r"[*?\[\\]")


@dataclass(frozen=True)
class Entry:
    """A file ("f"), folder ("d"), symlink ("l") or anything else ("o")."""

    kind: str
    size: int = 0
    mtime: int = 0
    mode: int = 0  # permission bits
    target: str = ""  # symlinks only
    # The name as stored. Entries are keyed by the NFC form, because macOS
    # may store a name decomposed (NFD) that Windows and Linux keep composed.
    path: str = field(default="", compare=False)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


@dataclass(frozen=True)
class SyncResult:
    remote: str  # project folder on the Mac
    uploaded: int  # files and symlinks sent
    deleted: int  # paths removed on the Mac
    kept_local: tuple[str, ...] = ()  # secrets that were not sent (see SECRETS)


def _patterns(lines: Iterable[str]) -> list:
    return [p for p in pathspec.GitIgnoreSpec.from_lines(list(lines)).patterns if p.include is not None]


def _match(patterns: Sequence, path: str) -> bool | None:
    """Last matching pattern wins, as in .gitignore: True ignores, False re-includes, None: no match."""
    result = None
    for pattern in patterns:
        if pattern.regex.match(path):
            result = pattern.include
    return result


_SECRET_PATTERNS = _patterns(SECRETS)


def _holds_private_key(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return _KEY_MARK in f.read(_KEY_READ)
    except OSError:
        return False


def _literal_dir(pattern: str) -> str:
    """The folder a pattern is confined to ("" when it can match anywhere)."""
    core = pattern.rstrip("/")
    if "/" not in core.lstrip("/"):
        return ""  # a bare name matches at any depth
    glob = _GLOB.search(core)
    literal = core[: glob.start()] if glob else core
    return literal.lstrip("/").rpartition("/")[0] + "/" if "/" in literal.lstrip("/") else ""


class ProjectFilter:
    """Decides which paths of a project are synced.

    Paths are relative and '/'-separated, and folders end in '/'. The rules,
    strongest first: `!pattern` lines in .nomacignore force paths in; other
    .nomacignore lines leave paths out; secrets (SECRETS, and key files that
    `scan` found by content) stay out; then the default skips and every
    .gitignore in the project. All patterns use .gitignore syntax.
    """

    def __init__(self, nomacignore: Iterable[str] = ()):
        forced: list[str] = []
        skipped: list[str] = []
        for raw in nomacignore:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                forced.append(line[1:].strip())
            else:
                skipped.append(line)
        self._forced = _patterns(forced)
        self._skipped = _patterns(skipped)
        self._forced_dirs = [_literal_dir(p) for p in forced]
        self._gitignores: dict[str, list] = {}  # folder ("" or "a/b/") -> its .gitignore
        self._key_files: set[str] = set()

    def add_gitignore(self, folder: str, lines: Iterable[str]) -> None:
        self._gitignores[folder] = _patterns(lines)

    def add_key_file(self, path: str) -> None:
        """A file that holds a private key: it stays local and is never removed from the Mac."""
        self._key_files.add(path)

    def secret(self, path: str) -> bool:
        # Any case: Windows and macOS file systems ignore it, so `.P12` is a `.p12`.
        return path in self._key_files or _match(_SECRET_PATTERNS, path.lower()) is True

    def forced(self, path: str) -> bool:
        return _match(self._forced, path) is True

    def may_contain_forced(self, folder: str) -> bool:
        return any(d.startswith(folder) or folder.startswith(d) for d in self._forced_dirs)

    def prune_names(self) -> list[str]:
        """Default skips the Mac can leave out of its listing (no forced path can lie inside)."""
        if "" in self._forced_dirs:
            return []
        needed = {part for d in self._forced_dirs for part in d.split("/")}
        return [name for name in DEFAULT_SKIPS if name not in needed]

    def _excluded(self, path: str, *, secrets: bool = True) -> bool:
        """The rules for `path` itself, ignoring its parent folders."""
        if _match(self._skipped, path) or (secrets and self.secret(path)):
            return True
        if path.endswith("/") and path.rstrip("/").rpartition("/")[2] in DEFAULT_SKIPS:
            return True
        parts = path.rstrip("/").split("/")[:-1]
        for depth in range(len(parts), -1, -1):  # deepest .gitignore first
            folder = "".join(f"{p}/" for p in parts[:depth])
            patterns = self._gitignores.get(folder)
            if patterns:
                decision = _match(patterns, path[len(folder) :])
                if decision is not None:
                    return decision
        return False

    def includes(self, path: str) -> bool:
        """Whether `path` is synced, given that a left-out folder leaves out all of its contents."""
        if self.forced(path):
            return True
        parts = path.rstrip("/").split("/")
        for i in range(1, len(parts)):
            if self._excluded("/".join(parts[:i]) + "/"):
                return False
        return not self._excluded(path)


@dataclass
class Scan:
    entries: dict[str, Entry] = field(default_factory=dict)  # relative path (no trailing /) -> Entry
    filter: ProjectFilter = field(default_factory=ProjectFilter)
    secrets: list[str] = field(default_factory=list)  # left out as secrets (folders end in /)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _git_executables(root: Path) -> dict[str, bool] | None:
    """Tracked files and whether Git records them as executable, or None outside a Git work tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-s", "-z"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    result = {}
    for record in out.split(b"\0"):
        meta, _, name = record.partition(b"\t")
        if name:
            result[name.decode("utf-8", "surrogateescape")] = meta.startswith(b"100755")
    return result


def _has_shebang(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def scan(root: Path, *, detect_executables: bool = True) -> Scan:
    """List what `sync` uploads from root, parents before children.

    Windows has no executable bit, so there a file counts as executable when
    Git records it as such or, for files Git does not track, when it starts
    with "#!". Elsewhere the file's own permission bits are used.
    """
    windows = sys.platform == "win32"
    result = Scan(filter=ProjectFilter(_read_lines(root / NOMACIGNORE)))
    filt = result.filter
    git = _git_executables(root) if windows and detect_executables else None

    def file_mode(path: str, full: Path, st: os.stat_result) -> int:
        if not windows:
            return stat.S_IMODE(st.st_mode)
        if not detect_executables:
            return 0o644
        tracked = git.get(path) if git is not None else None
        executable = tracked if tracked is not None else _has_shebang(full)
        return 0o755 if executable else 0o644

    def visit(folder: Path, prefix: str, stored_prefix: str, inside_excluded: bool) -> None:
        if (folder / GITIGNORE).is_file():
            filt.add_gitignore(prefix, _read_lines(folder / GITIGNORE))
        try:
            items = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError as e:
            raise TransferError(f"cannot read {folder}: {e}") from e
        for item in items:
            path = prefix + _nfc(item.name)
            stored = stored_prefix + item.name
            if windows and item.is_junction():
                continue
            is_dir = item.is_dir(follow_symlinks=False)
            key = path + "/" if is_dir else path
            if inside_excluded:
                included = filt.forced(key)
            else:
                forced = filt.forced(key)
                if not forced and not is_dir and key.lower().endswith(KEY_SUFFIXES) and _holds_private_key(Path(item.path)):
                    filt.add_key_file(key)
                included = forced or not filt._excluded(key)
                if not included and filt.secret(key) and not filt._excluded(key, secrets=False):
                    result.secrets.append(key)  # only these would have gone otherwise
            if is_dir:
                if included:
                    st = item.stat(follow_symlinks=False)
                    mode = 0o755 if windows else stat.S_IMODE(st.st_mode)
                    result.entries[path] = Entry("d", mtime=int(st.st_mtime), mode=mode, path=stored)
                    visit(Path(item.path), key, stored + "/", inside_excluded and not filt.forced(key))
                elif filt.may_contain_forced(key):
                    visit(Path(item.path), key, stored + "/", True)
                continue
            if not included:
                continue
            st = item.stat(follow_symlinks=False)
            if item.is_symlink():
                target = os.readlink(item.path).replace("\\", "/") if windows else os.readlink(item.path)
                result.entries[path] = Entry("l", len(os.fsencode(target)), int(st.st_mtime), 0o777, target, stored)
            elif item.is_file(follow_symlinks=False):
                mode = file_mode(stored, Path(item.path), st)
                result.entries[path] = Entry("f", st.st_size, int(st.st_mtime), mode, path=stored)

    visit(root, "", "", False)
    return result


def remote_script(remote: str, prune: Sequence[str]) -> str:
    """POSIX sh for the Mac: list `remote`, delete what stdin names, unpack the tar that follows.

    The listing is BEGIN, NUL-terminated names from find, one more NUL, one
    `stat` line (mtime, size, octal mode) per name, then END. The script then
    reads two decimal byte counts, each followed by that many bytes of
    NUL-terminated paths: first paths to `rm -rf`, then folders to `rmdir`
    (deepest first; non-empty ones stay). `read` and `dd bs=1` take exactly
    what they need from stdin, so tar gets the rest.
    """
    q = shlex.quote
    prune_expr = ""
    if prune:
        tests = " -o ".join(f"-name {q(name)}" for name in prune)
        prune_expr = f"-type d \\( {tests} \\) -prune -o "
    return f"""set -e
mkdir -p -- {q(remote)}
cd -- {q(remote)}
t=$(mktemp -t qem)
trap 'rm -f -- "$t" "$t.rm" "$t.rmdir"' EXIT
find . -mindepth 1 {prune_expr}-print0 > "$t"
printf 'qem-manifest\\n'
cat "$t"
printf '\\0'
if [ -s "$t" ]; then xargs -0 stat -f '%m %z %p' < "$t"; fi
printf 'qem-manifest-end\\n'
IFS= read -r n; dd bs=1 count="$n" of="$t.rm" 2>/dev/null
IFS= read -r n; dd bs=1 count="$n" of="$t.rmdir" 2>/dev/null
if [ -s "$t.rm" ]; then xargs -0 rm -rf -- < "$t.rm"; fi
if [ -s "$t.rmdir" ]; then xargs -0 rmdir -- < "$t.rmdir" 2>/dev/null || true; fi
tar -xpf -
"""


def remote_command(remote: str, prune: Sequence[str]) -> str:
    # ssh hands the command to the user's login shell (zsh); run the script in sh.
    return "/bin/sh -c " + shlex.quote(remote_script(remote, prune))


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "f"
    if stat.S_ISDIR(mode):
        return "d"
    if stat.S_ISLNK(mode):
        return "l"
    return "o"


def parse_manifest(data: bytes) -> dict[str, Entry]:
    start = data.find(BEGIN)
    if start < 0 or not data.endswith(END):
        raise TransferError("the Mac sent no file listing")
    body = data[start + len(BEGIN) : -len(END)]
    if body.startswith(b"\0"):
        names: list[bytes] = []
        rest = body[1:]
    else:
        cut = body.find(b"\0\0")
        if cut < 0:
            raise TransferError("the Mac sent a malformed file listing")
        names = body[:cut].split(b"\0")
        rest = body[cut + 2 :]
    lines = rest.decode("ascii").splitlines()
    if len(lines) != len(names):
        raise TransferError("the project folder on the Mac changed while it was listed; sync again")
    entries = {}
    for raw, line in zip(names, lines, strict=True):
        name = raw.decode("utf-8", "surrogateescape")
        parts = name.split("/")
        if parts[0] != "." or len(parts) < 2 or any(p in ("", ".", "..") for p in parts[1:]):
            raise TransferError(f"the Mac listed an unexpected path: {name!r}")
        mtime, size, mode = line.split()
        mode_bits = int(mode, 8)
        stored = "/".join(parts[1:])
        entries[_nfc(stored)] = Entry(_kind(mode_bits), int(size), int(mtime), stat.S_IMODE(mode_bits), path=stored)
    return entries


@dataclass(frozen=True)
class Plan:
    upload: list[str]  # local paths to send, parents first
    remove: list[str]  # Mac paths to rm -rf before unpacking
    rmdirs: list[str]  # Mac folders to remove if empty, deepest first


def _differs(mine: Entry, theirs: Entry) -> bool:
    if mine.kind == "f":
        return (mine.size, mine.mtime, mine.mode) != (theirs.size, theirs.mtime, theirs.mode)
    if mine.kind == "l":
        return mine.size != theirs.size
    return False


def plan(local: Mapping[str, Entry], remote: Mapping[str, Entry], filt: ProjectFilter, *, delete: bool) -> Plan:
    """What to send and remove. Paths the filter leaves out are never removed from the Mac."""
    upload: list[str] = []
    remove: list[str] = []
    rmdirs: list[str] = []
    for path, mine in local.items():
        theirs = remote.get(path)
        if theirs is not None and theirs.kind != mine.kind:
            remove.append(theirs.path or path)  # a file became a folder or the other way round
            theirs = None
        if theirs is None or _differs(mine, theirs):
            upload.append(path)
    if delete:
        for path, theirs in remote.items():
            if path in local or not filt.includes(path + "/" if theirs.kind == "d" else path):
                continue
            (rmdirs if theirs.kind == "d" else remove).append(theirs.path or path)
        rmdirs.sort(key=lambda p: p.count("/"), reverse=True)
    return Plan(upload, remove, rmdirs)


def removal_request(todo: Plan) -> bytes:
    out = b""
    for paths in (todo.remove, todo.rmdirs):
        data = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)
        out += f"{len(data)}\n".encode() + data
    return out


def write_tar(stream: IO[bytes], root: Path, entries: Mapping[str, Entry], paths: Sequence[str]) -> None:
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for path in paths:
            entry = entries[path]
            stored = entry.path or path
            info = tarfile.TarInfo(stored)
            info.mtime = entry.mtime
            info.mode = entry.mode
            if entry.kind == "d":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif entry.kind == "l":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.target
                tar.addfile(info)
            else:
                info.size = entry.size
                try:
                    with open(root / stored, "rb") as f:
                        tar.addfile(info, f)
                except OSError as e:
                    raise TransferError(f"cannot upload {path} (did it change during sync?): {e}") from e


def read_manifest(stream: IO[bytes]) -> dict[str, Entry]:
    data = b""
    while not data.endswith(END):
        chunk = stream.read1(65536)
        if not chunk:
            break
        data += chunk
    return parse_manifest(data)


def run(argv: list[str], env: Mapping[str, str], project: Path, remote: str, found: Scan, *, delete: bool) -> SyncResult:
    """Run the ssh `argv` (which runs `remote_command`) and drive the exchange with the Mac."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        failure: Exception | None = None
        todo = Plan([], [], [])
        try:
            todo = plan(found.entries, read_manifest(proc.stdout), found.filter, delete=delete)
            proc.stdin.write(removal_request(todo))
            write_tar(proc.stdin, project, found.entries, todo.upload)
        except (TransferError, OSError) as e:
            failure = e
        finally:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        returncode = proc.wait()
        errors.seek(0)
        detail = errors.read().decode(errors="replace").strip()
    if failure is None and returncode == 0:
        uploaded = sum(1 for p in todo.upload if found.entries[p].kind != "d")
        return SyncResult(remote, uploaded, len(todo.remove) + len(todo.rmdirs), tuple(found.secrets))
    # Report our own reason first; ssh's stderr explains a failed connection.
    message = f"sync to {remote} failed: {failure}" if failure else f"sync to {remote} failed (exit code {returncode})"
    if returncode != 0 and detail:
        message += f": {detail}"
    raise TransferError(message) from failure
