# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Browser-approved device login: nothing secret is typed in the terminal."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from .api import Transport
from .errors import ApiError, LoginFailed, WaitTimeout

DEFAULT_INTERVAL = 5  # RFC 8628's default when the server names none
DEFAULT_EXPIRES_IN = 600  # used only if the server names no lifetime
MIN_INTERVAL = 1  # never poll in a tight loop, whatever the server says
SLOW_DOWN_STEP = 5  # RFC 8628: slow_down adds 5 s to the interval for good

_FAILURES = {
    "denied": "the login was denied in the browser",
    "expired": "the login code expired before it was approved",
    "claimed": "this login code was already used to create a key",
    "": "NoMac answered the login poll without a status",
}

# Error codes a plain RFC 8628 server sends instead of a status.
_RFC_ERRORS = {
    "authorization_pending": "pending",
    "slow_down": "slow_down",
    "access_denied": "denied",
    "expired_token": "expired",
}


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    url: str
    interval: int
    expires_in: int


class DeviceLogin:
    """Browser-approved login (OAuth 2.0 device authorization, RFC 8628 style).

    Facts (docs/protocol.md): POST /device/start with {"client_name"} answers
    device_code, user_code, verification_uri, verification_uri_complete,
    interval and expires_in. POST /device/token with {"device_code"} answers a
    status: pending, slow_down, approved (with api_key), denied, expired, claimed.
    Both calls go without an API key (`auth=False`).

    Interface (used by the CLI and tests):
    - `DeviceLogin(transport)`
    - `start(client_name=None) -> DeviceCode`. The default client_name is
      "qem on <hostname>".
    - `poll(code, *, sleep=time.sleep, clock=time.monotonic) -> str`: block until
      approved and return the API key. denied/expired/claimed raise LoginFailed;
      running out of time raises WaitTimeout. Follow RFC 8628 for the polling
      interval and for slow_down.
    """

    def __init__(self, transport: Transport):
        self._transport = transport

    def start(self, client_name: str | None = None) -> DeviceCode:
        """Ask for a code; the account owner approves it at `DeviceCode.url`."""
        name = client_name or f"qem on {socket.gethostname() or 'this computer'}"
        body = self._transport.request(
            "POST", "/device/start", json={"client_name": name}, auth=False
        )
        device_code = body.get("device_code")
        user_code = body.get("user_code")
        url = body.get("verification_uri_complete") or body.get("verification_uri")
        if not (isinstance(device_code, str) and device_code and user_code and url):
            raise LoginFailed(
                "the login could not start: NoMac returned no device code"
            )
        return DeviceCode(
            device_code=device_code,
            user_code=str(user_code),
            url=str(url),
            interval=_seconds(body.get("interval"), DEFAULT_INTERVAL),
            expires_in=_seconds(body.get("expires_in"), DEFAULT_EXPIRES_IN),
        )

    def poll(
        self,
        code: DeviceCode,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> str:
        """Wait until the code is approved and return the new API key."""
        # RFC 8628 3.5: wait `interval` between requests, and after slow_down
        # add 5 s to it for this and every later request.
        interval = max(code.interval, MIN_INTERVAL)
        deadline = clock() + code.expires_in
        while True:
            if clock() + interval > deadline:
                raise WaitTimeout(
                    f"the login code was not approved within {code.expires_in} s"
                )
            sleep(interval)
            status, body = self._status(code.device_code)
            if status == "approved":
                key = body.get("api_key")
                if not isinstance(key, str) or not key:
                    raise LoginFailed(
                        "the login was approved, but NoMac returned no API key"
                    )
                return key
            if status == "slow_down":
                interval += SLOW_DOWN_STEP
            elif status != "pending":
                raise LoginFailed(_FAILURES.get(status, f"the login failed ({status})"))

    def _status(self, device_code: str) -> tuple[str, dict]:
        try:
            body = self._transport.request(
                "POST", "/device/token", json={"device_code": device_code}, auth=False
            )
        except ApiError as e:
            # RFC 8628 puts the code in a plain string: {"error": "slow_down"}.
            error = e.body.get("error")
            code = error if isinstance(error, str) else e.code
            if code in _RFC_ERRORS:
                return _RFC_ERRORS[code], {}
            raise
        return str(body.get("status") or ""), body


def _seconds(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return default
