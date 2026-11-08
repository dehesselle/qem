# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Python library and CLI for NoMac cloud Macs (https://nomac.app).

from qem import Nomac

with Nomac() as nm, nm.start(max_duration=1800, max_cost=1) as mac:
    print(mac.exec(["sw_vers"]).stdout)
"""

from .client import Nomac
from .errors import (
    ApiError,
    LoginFailed,
    NetworkError,
    NomacError,
    NotFound,
    NotLoggedIn,
    OutputExpired,
    PaymentRequired,
    PreconditionFailed,
    TransferError,
    TunnelError,
    Unauthorized,
    WaitTimeout,
)
from .models import Access, Job, Login, OutputChunk, SessionInfo
from .session import MacSession
from .transfer import PullResult
from .upload import SyncResult
from .version_info import version
from .vnc import DesktopTunnel

__version__ = version()

__all__ = [
    "Access",
    "ApiError",
    "DesktopTunnel",
    "Job",
    "Login",
    "LoginFailed",
    "MacSession",
    "NetworkError",
    "Nomac",
    "NomacError",
    "NotFound",
    "NotLoggedIn",
    "OutputChunk",
    "OutputExpired",
    "PaymentRequired",
    "PreconditionFailed",
    "PullResult",
    "SessionInfo",
    "SyncResult",
    "TransferError",
    "TunnelError",
    "Unauthorized",
    "WaitTimeout",
    "__version__",
]
