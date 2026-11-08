# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Thin HTTP layer over https://nomac.app/api/v1."""

from __future__ import annotations

from typing import Any

import httpx

from . import config
from .errors import NetworkError, NotLoggedIn, api_error
from .version_info import version

API_PREFIX = "/api/v1"

# Long polls wait up to 50 s on the server; leave room on top of that.
DEFAULT_TIMEOUT = httpx.Timeout(15.0, read=75.0)


def _user_agent() -> str:
    return f"qem/{version()}"


class Transport:
    """Sends requests with Bearer auth and turns error envelopes into exceptions."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
    ):
        self.api_key = config.resolve_api_key(api_key)
        self.base_url = config.api_url(base_url)
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._client.headers["User-Agent"] = _user_agent()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth:
            if not self.api_key:
                raise NotLoggedIn("no API key: run `qem login` or set NOMAC_API_KEY")
            headers["Authorization"] = f"Bearer {self.api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{API_PREFIX}{path}",
                json=json,
                params=params,
                headers=headers,
            )
        except httpx.TransportError as e:
            raise NetworkError(f"{method} {path}: {e}") from e

        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.is_error:
            raise api_error(response.status_code, body)
        return body if isinstance(body, dict) else {"data": body}
