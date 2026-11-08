"""Typed views of API responses.

Only fields the library acts on are lifted out; `raw` keeps the whole
response so callers can read anything else without waiting for a release.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

LIVE_STATES = ("queued", "provisioning", "ready")
STARTING_STATES = ("queued", "provisioning")
ENDED_STATES = ("stopped", "failed")


def unwrap(body: dict[str, Any], key: str) -> dict[str, Any]:
    """Return body[key] when the API wraps the object, else the body itself."""
    inner = body.get(key)
    return inner if isinstance(inner, dict) else body


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _time(value: Any) -> datetime | None:
    """API times come as epoch milliseconds or ISO 8601 strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SessionInfo:
    id: str
    state: str
    cost_microunits: int | None = None
    idle_deadline_at: datetime | None = None
    hard_deadline_at: datetime | None = None
    failure_code: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> SessionInfo:
        s = unwrap(body, "session")
        activity = s.get("activity") or {}
        return cls(
            id=str(s.get("id", "")),
            state=str(s.get("state", "unknown")),
            cost_microunits=_int((s.get("cost") or {}).get("accrued_microunits")),
            idle_deadline_at=_time(activity.get("idle_deadline_at")),
            hard_deadline_at=_time(activity.get("hard_deadline_at")),
            failure_code=s.get("failure_code"),
            raw=s,
        )

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def ended(self) -> bool:
        return self.state in ENDED_STATES


@dataclass(frozen=True)
class Job:
    """A command on the Mac. `stdout`/`stderr` hold the first 64 KiB once it exited."""

    id: str
    state: str
    exit_code: int | None = None
    reason: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> Job:
        j = unwrap(body, "job")
        exit_code = j.get("exit_code")
        return cls(
            id=str(j.get("id") or j.get("job_id") or ""),
            state=str(j.get("state", "unknown")),
            exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            reason=j.get("reason"),
            stdout=j.get("stdout"),
            stderr=j.get("stderr"),
            raw=j,
        )

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def exited(self) -> bool:
        return self.state == "exited"

    def next_cursor(self, stream: str) -> int | None:
        """Where to resume reading `stream` when the inline output was cut off."""
        return _int(self.raw.get(f"{stream}_next_cursor"))


@dataclass(frozen=True)
class OutputChunk:
    data: bytes
    next_cursor: int
    eof: bool
    tail: bytes | None = None
    tail_offset: int | None = None
    total_bytes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, body: dict[str, Any], cursor: int) -> OutputChunk:
        data = base64.b64decode(body.get("data_base64") or "")
        next_cursor = _int(body.get("next_cursor"))
        tail = body.get("tail_base64")
        return cls(
            data=data,
            next_cursor=next_cursor if next_cursor is not None else cursor + len(data),
            eof=bool(body.get("eof")),
            tail=base64.b64decode(tail) if tail else None,
            tail_offset=_int(body.get("tail_offset")),
            total_bytes=_int(body.get("total_bytes")),
            raw=body,
        )


@dataclass(frozen=True)
class Login:
    """The Mac's login for this session, which Screen Sharing also accepts. Never written to disk."""

    user: str
    password: str = field(repr=False)
    variable: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> Login:
        return cls(
            user=str(body.get("user") or "agent"),
            password=str(body.get("password", "")),
            variable=body.get("variable"),
            raw=body,
        )


@dataclass(frozen=True)
class Access:
    """Short-lived SSH gateway credentials. The token is never written to disk."""

    session_id: str
    generation: int
    username: str
    host_key: str
    gateway: str
    token: str
    expires_at: str | None = None

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> Access:
        ssh = body.get("ssh") or {}
        return cls(
            session_id=str(body.get("session_id", "")),
            generation=_int(body.get("generation")) or 0,
            username=str(ssh.get("username") or "agent"),
            host_key=str(ssh.get("host_key", "")),
            gateway=str(body.get("gateway", "")),
            token=str(body.get("token", "")),
            expires_at=body.get("expires_at"),
        )
