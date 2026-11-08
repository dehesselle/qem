"""Exceptions raised by the library. The CLI maps them to messages and exit codes."""

from __future__ import annotations

from typing import Any


class NomacError(Exception):
    """Base class for every error raised by qem."""


class NotLoggedIn(NomacError):
    """No API key was given, found in NOMAC_API_KEY, or stored by `login`."""


class NetworkError(NomacError):
    """The request did not reach the API or no response came back.

    The operation may or may not have happened. Retry with the same
    request key, never with a new one.
    """


class ApiError(NomacError):
    """The API answered with an error envelope: {"error": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str, body: dict[str, Any] | None = None):
        super().__init__(f"{message} ({code})" if message else code)
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}


class Unauthorized(ApiError):
    """401: the API key is missing, wrong or revoked."""


class PaymentRequired(ApiError):
    """402: no credit, or Mac use is paused."""


class NotFound(ApiError):
    """404: no such session, job or resource."""


class OutputExpired(ApiError):
    """410: old job output was removed. The command must not be rerun because of it."""


class PreconditionFailed(ApiError):
    """422: invalid input or a failed precondition."""


class LoginFailed(NomacError):
    """The device login was denied, expired or already claimed."""


class WaitTimeout(NomacError):
    """Waiting for a session or job state ran out of time."""


class TunnelError(NomacError):
    """The SSH-over-WebSocket tunnel could not be opened or broke."""


class TransferError(NomacError):
    """ssh-keygen or ssh failed or is missing, or a sync or pull failed."""


_BY_STATUS: dict[int, type[ApiError]] = {
    401: Unauthorized,
    402: PaymentRequired,
    404: NotFound,
    410: OutputExpired,
    422: PreconditionFailed,
}


def api_error(status: int, body: Any) -> ApiError:
    """Build the matching ApiError subclass from a response status and JSON body."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or f"http_{status}")
        message = str(error.get("message") or "")
    else:
        code, message = f"http_{status}", ""
    cls = _BY_STATUS.get(status, ApiError)
    return cls(status, code, message, body if isinstance(body, dict) else None)
