# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from qem import Nomac


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real qem folder and API key."""
    home = tmp_path / "qem-home"
    monkeypatch.setenv("QEM_HOME", str(home))
    monkeypatch.setenv("NOMAC_API_KEY", "nmk_test")
    monkeypatch.delenv("NOMAC_API_URL", raising=False)
    return home


class FakeApi:
    """Route table for httpx.MockTransport; records every request."""

    def __init__(self):
        self.routes: dict[
            tuple[str, str], Callable[[httpx.Request], httpx.Response] | list
        ] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, *responses: dict | tuple[int, dict]) -> None:
        """Answer with the given bodies in order; the last one repeats."""
        self.routes[(method, "/api/v1" + path)] = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": f"no route {key}"}}
            )
        queue = self.routes[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    def body(self, index: int) -> dict:
        return json.loads(self.requests[index].content)

    def paths(self) -> list[str]:
        return [
            f"{r.method} {r.url.path.removeprefix('/api/v1')}" for r in self.requests
        ]


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def make_client(api):
    def make(**kwargs) -> Nomac:
        return Nomac(
            http_client=httpx.Client(transport=httpx.MockTransport(api)),
            sleep=lambda s: None,
            **kwargs,
        )

    return make
