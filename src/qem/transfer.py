# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Where projects live on the Mac, and pull (tar over the session's ssh).

`sync` is in upload.py. Neither direction uses rsync; see docs/DESIGN.md.
"""

from __future__ import annotations

import shlex
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from .errors import TransferError

REMOTE_ROOT = "/Users/agent/drive/src"


def remote_name(local: Path) -> str:
    """Folder name on the Mac's drive for a local project folder: the folder's own
    name, in NFC. Every shell use quotes it, so no character needs replacing."""
    name = unicodedata.normalize("NFC", local.resolve().name)
    if not name:
        raise TransferError(
            f"{local} has no folder name: sync a folder, not a drive root"
        )
    if len(name.encode("utf-8")) > 255:
        raise TransferError(
            f"folder name too long for macOS (over 255 bytes): {name[:40]}..."
        )
    return name


def remote_dir(name: str) -> str:
    return f"{REMOTE_ROOT}/{name}"


def resolve_remote(path: str, project: str) -> str:
    """Absolute and ~ paths stay as they are; anything else is inside the project folder."""
    if path.startswith(("/", "~")):
        return path
    return str(PurePosixPath(remote_dir(project)) / path)


@dataclass(frozen=True)
class PullResult:
    dest: Path
    written: int  # files written
    kept: int  # files that existed locally and were left alone


def _remote_path(path: str) -> str:
    """Quote a Mac path for its shell, keeping a leading ~ expandable."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def pull_command(remote: str) -> str:
    """Shell command for the Mac that writes `remote` (file or folder) as a tar stream.

    COPYFILE_DISABLE keeps macOS tar from adding ._ AppleDouble files.
    """
    path = PurePosixPath(remote.rstrip("/") or "/")
    return (
        f"cd -- {_remote_path(str(path.parent))} && "
        f"COPYFILE_DISABLE=1 exec tar -cf - -- {shlex.quote(path.name)}"
    )


def extract(stream: IO[bytes], dest: Path, *, overwrite: bool) -> tuple[int, int]:
    """Unpack a tar stream into dest. Returns (files written, existing files kept).

    The "data" filter refuses absolute paths, links out of dest and device
    files, so the Mac cannot write outside dest.
    """
    written = kept = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            target = dest / member.name
            if not member.isdir():
                if target.exists() or target.is_symlink():
                    if not overwrite:
                        kept += 1
                        continue
                    if target.is_symlink() or member.issym():
                        target.unlink()  # never write through an old link
                written += 1
            archive.extract(member, dest, filter="data")
    return written, kept
