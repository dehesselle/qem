# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Where the API key and local files live.

Everything is kept under ~/.qem (override with QEM_HOME), separate
from the npm CLI's ~/.nomac whose formats are not documented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_API_URL = "https://nomac.app"


def home() -> Path:
    return Path(os.environ.get("QEM_HOME") or Path.home() / ".qem")


def credentials_path() -> Path:
    return home() / "credentials.json"


def api_url(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("NOMAC_API_URL") or DEFAULT_API_URL).rstrip("/")


def load_api_key() -> str | None:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = data.get("api_key") if isinstance(data, dict) else None
    return key if isinstance(key, str) and key else None


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Explicit argument first, then NOMAC_API_KEY, then the stored key."""
    return explicit or os.environ.get("NOMAC_API_KEY") or load_api_key()


def write_private(path: Path, text: str) -> None:
    """Write a file readable only by the owner, replacing it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save_api_key(key: str) -> Path:
    path = credentials_path()
    write_private(path, json.dumps({"api_key": key}) + "\n")
    return path


def delete_api_key() -> bool:
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        return False
    return True
