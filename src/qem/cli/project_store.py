# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Which session belongs to which project folder (CLI only)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import config


def sessions_root() -> Path:
    """Where per-project session state lives, under `config.home()`."""
    return config.home() / "projects"


def _key(folder: Path) -> str:
    # normcase: on Windows C:\Code\App and c:\code\app are the same folder.
    return hashlib.sha256(os.path.normcase(str(folder)).encode()).hexdigest()[:24]


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


class ProjectStore:
    """Which session belongs to which project folder.

    Requirements: the state is kept outside the project folder (in
    `sessions_root()`), so nothing checked into a repository can point qem at
    another Mac or key. One project folder (resolved real path) maps to one
    record. Interface used elsewhere:
    - `ProjectStore(project: Path | None = None)` (default: the current folder);
      attributes `project` (the resolved folder) and `path` (the state file).
    - `load() -> dict | None`: the saved record, or None when there is none, it
      is unreadable, or it has no "id".
    - `save(data: dict)`: write the record privately (`config.write_private`),
      including the project folder.
    - `update(**changes) -> dict`: merge into the record, save, return it.
    - `clear()`: forget the record (and its folder when empty).
    """

    def __init__(self, project: Path | None = None):
        self.project = Path(project or Path.cwd()).resolve()
        # One flat file per folder, named by a hash of its path:
        # <qem home>/projects/<24 hex digits>.json
        self.path = sessions_root() / f"{_key(self.project)}.json"

    def __repr__(self) -> str:
        return f"ProjectStore({str(self.project)!r})"

    def load(self) -> dict[str, Any] | None:
        data = _read(self.path)
        if data is None:
            return None
        recorded = data.get("project")
        if isinstance(recorded, str) and os.path.normcase(recorded) != os.path.normcase(
            str(self.project)
        ):
            return None  # a record that belongs to another folder
        return data

    def save(self, data: dict[str, Any]) -> None:
        record = {**data, "project": str(self.project)}
        config.write_private(self.path, json.dumps(record, indent=2) + "\n")

    def update(self, **changes: Any) -> dict[str, Any]:
        data = {**(self.load() or {}), **changes}
        self.save(data)
        return {**data, "project": str(self.project)}

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        try:
            self.path.parent.rmdir()  # only succeeds once no project has a record
        except OSError:
            pass


def find_identity(session_id: str) -> str | None:
    """The key path a session was started with, from whichever project's record has it."""
    for path in sorted(sessions_root().glob("*.json")):
        data = _read(path)
        identity = (
            data.get("identity") if data and data.get("id") == session_id else None
        )
        if isinstance(identity, str) and identity and Path(identity).is_file():
            return identity
    return None
