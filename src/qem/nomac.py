# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Entry point of the library: account calls and starting Macs."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import httpx

from .errors import ApiError
from .mac_session import MacSession, new_request_key
from .models import SessionInfo
from .ssh import generate_identity, keys_dir, remove_identity
from .transport import Transport


class Nomac:
    """Client for one account.

    The API key is taken from `api_key`, then NOMAC_API_KEY, then what
    `qem login` stored.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.transport = Transport(api_key, base_url, client=http_client)
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def me(self) -> dict[str, Any]:
        return self.transport.request("GET", "/me")

    def credits(self) -> dict[str, Any]:
        return self.transport.request("GET", "/mac/credits")

    def sessions(self) -> list[SessionInfo]:
        body = self.transport.request("GET", "/mac/sessions")
        items = body.get("sessions", body.get("data", []))
        return [SessionInfo.from_json(item) for item in items if isinstance(item, dict)]

    def live_sessions(self) -> list[SessionInfo]:
        return [s for s in self.sessions() if s.live]

    def session(
        self, session_id: str, *, identity: Path | str | None = None
    ) -> MacSession:
        """Handle to an existing session. ssh needs the `identity` it was started with."""
        return MacSession(
            self.transport,
            session_id,
            identity=Path(identity) if identity else None,
            sleep=self._sleep,
            clock=self._clock,
        )

    def start(
        self,
        *,
        max_duration: int = 2 * 3600,
        idle_timeout: int = 600,
        max_cost: float = 5.0,
        parallel: bool = False,
        request_key: str | None = None,
        identity: Path | str | None = None,
        ssh: bool = True,
        wait: float | None = 240,
    ) -> MacSession:
        """Start a Mac and (unless wait is None) block until it is ready.

        With `ssh` and no `identity`, an ed25519 key is made under
        ~/.qem/keys, kept on the returned session and deleted by `stop()`.
        Its name follows from the request key, so a retry after a NetworkError
        with the same `request_key` sends the same public key, as the
        idempotent API requires. A start while a Mac already runs raises
        ApiError (409) with code "session_limit_reached"; `live_sessions()`
        finds the running one. Durations are in seconds, `max_cost` in dollars.
        """
        request_key = request_key or new_request_key()
        generated = None
        if ssh and identity is None:
            identity = (
                keys_dir()
                / f"id_ed25519_{hashlib.sha256(request_key.encode()).hexdigest()[:32]}"
            )
            if not identity.exists():
                generated = generate_identity(identity)
        body: dict[str, Any] = {
            "profile": "standard",
            "limits": {
                "max_duration_seconds": int(max_duration),
                "idle_timeout_seconds": int(idle_timeout),
                "spend_cap_microunits": str(round(max_cost * 1_000_000)),
            },
        }
        if identity:
            body["public_key"] = (
                Path(f"{identity}.pub").read_text(encoding="utf-8").strip()
            )
        if parallel:
            body["parallel"] = True
        try:
            info = SessionInfo.from_json(
                self.transport.request(
                    "POST", "/mac/sessions", json=body, idempotency_key=request_key
                )
            )
        except ApiError:
            # A definite refusal: no Mac will ever hold this key. (After a
            # NetworkError the start may have happened, so the key stays.)
            remove_identity(generated)
            raise
        mac = MacSession(
            self.transport,
            info.id,
            identity=Path(identity) if identity else None,
            info=info,
            sleep=self._sleep,
            clock=self._clock,
        )
        if wait is not None and not info.ready:
            mac.wait_ready(wait)
        return mac
