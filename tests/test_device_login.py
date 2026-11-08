# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import httpx
import pytest

from qem import config
from qem.device_login import DeviceCode, DeviceLogin
from qem.errors import (
    ApiError,
    LoginFailed,
    NetworkError,
    NotLoggedIn,
    OutputExpired,
    PaymentRequired,
    PreconditionFailed,
    WaitTimeout,
)
from qem.transport import Transport


def transport(api, **kwargs):
    return Transport(client=httpx.Client(transport=httpx.MockTransport(api)), **kwargs)


def test_headers_and_base_url(api):
    api.on("POST", "/mac/sessions", {"session": {"id": "ses_1"}})
    transport(api).request("POST", "/mac/sessions", json={}, idempotency_key="k-1")
    req = api.requests[0]
    assert str(req.url) == "https://nomac.app/api/v1/mac/sessions"
    assert req.headers["Authorization"] == "Bearer nmk_test"
    assert req.headers["Idempotency-Key"] == "k-1"
    assert req.headers["User-Agent"].startswith("qem/")


def test_base_url_from_env(api, monkeypatch):
    monkeypatch.setenv("NOMAC_API_URL", "http://127.0.0.1:9/")
    api.on("GET", "/me", {})
    transport(api).request("GET", "/me")
    assert str(api.requests[0].url) == "http://127.0.0.1:9/api/v1/me"


@pytest.mark.parametrize(
    "status, cls",
    [
        (402, PaymentRequired),
        (410, OutputExpired),
        (422, PreconditionFailed),
        (500, ApiError),
    ],
)
def test_error_envelope(api, status, cls):
    api.on(
        "GET",
        "/me",
        (status, {"error": {"code": "some_code", "message": "Some message"}}),
    )
    with pytest.raises(cls) as exc:
        transport(api).request("GET", "/me")
    assert exc.value.code == "some_code"
    assert exc.value.status == status
    assert "Some message" in str(exc.value)


def test_network_error():
    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(NetworkError):
        transport(boom).request("GET", "/me")


def test_no_key(api, monkeypatch):
    monkeypatch.delenv("NOMAC_API_KEY")
    with pytest.raises(NotLoggedIn):
        transport(api).request("GET", "/me")
    assert api.requests == []


def test_key_order(monkeypatch):
    config.save_api_key("nmk_stored")
    assert config.resolve_api_key("nmk_arg") == "nmk_arg"
    assert config.resolve_api_key() == "nmk_test"
    monkeypatch.delenv("NOMAC_API_KEY")
    assert config.resolve_api_key() == "nmk_stored"


def test_home_is_the_user_config_folder(monkeypatch, isolated_home, tmp_path):
    assert config.home() == isolated_home  # QEM_HOME wins
    asked = []

    def user_config_dir(appname, appauthor=None):
        asked.append((appname, appauthor))
        return str(tmp_path / "config" / appname)

    monkeypatch.setattr("platformdirs.user_config_dir", user_config_dir)
    monkeypatch.delenv("QEM_HOME")
    assert config.home() == tmp_path / "config" / "qem"
    # no vendor folder on Windows (%LOCALAPPDATA%\qem, not ...\qem\qem)
    assert asked == [("qem", False)]


def test_device_login(api):
    api.on(
        "POST",
        "/device/start",
        {
            "device_code": "dc",
            "user_code": "ABCD-1234",
            "verification_uri": "https://nomac.app/device",
            "verification_uri_complete": "https://nomac.app/device?code=ABCD-1234",
            "interval": 1,
            "expires_in": 60,
        },
    )
    api.on(
        "POST",
        "/device/token",
        {"status": "pending"},
        {"status": "slow_down"},
        {"status": "pending"},
        {"status": "approved", "api_key": "nmk_new"},
    )
    sleeps = []
    login = DeviceLogin(transport(api))
    code = login.start("my laptop")
    assert code == DeviceCode(
        "dc", "ABCD-1234", "https://nomac.app/device?code=ABCD-1234", 1, 60
    )
    assert login.poll(code, sleep=sleeps.append) == "nmk_new"
    # RFC 8628: wait the interval before each poll; slow_down adds 5 s for good
    assert sleeps == [1, 1, 6, 6]
    assert api.body(0) == {"client_name": "my laptop"}
    assert api.body(1) == {"device_code": "dc"}
    assert all("Authorization" not in r.headers for r in api.requests)


def test_device_login_defaults(api, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "box")
    api.on(
        "POST",
        "/device/start",
        {
            "device_code": "dc",
            "user_code": "C",
            "verification_uri": "https://nomac.app/device",
        },
    )
    code = DeviceLogin(transport(api)).start()
    assert api.body(0) == {"client_name": "qem on box"}
    assert (code.url, code.interval) == (
        "https://nomac.app/device",
        5,
    )  # RFC 8628's default interval
    assert code.expires_in > 0


def test_device_login_start_without_code(api):
    api.on("POST", "/device/start", {"user_code": "C"})
    with pytest.raises(LoginFailed):
        DeviceLogin(transport(api)).start("x")


@pytest.mark.parametrize("status", ["denied", "expired", "claimed", "something_new"])
def test_device_login_fails(api, status):
    api.on("POST", "/device/token", {"status": status})
    code = DeviceCode("dc", "X", "u", 5, 60)
    with pytest.raises(LoginFailed):
        DeviceLogin(transport(api)).poll(code, sleep=lambda s: None)


def test_device_login_approved_without_key(api):
    api.on("POST", "/device/token", {"status": "approved"})
    with pytest.raises(LoginFailed, match="no API key"):
        DeviceLogin(transport(api)).poll(
            DeviceCode("dc", "X", "u", 5, 60), sleep=lambda s: None
        )


def test_device_login_runs_out_of_time(api):
    api.on(
        "POST",
        "/device/token",
        {"status": "pending"},
        {"status": "slow_down"},
        {"status": "pending"},
    )
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    code = DeviceCode("dc", "X", "u", 5, 30)
    with pytest.raises(WaitTimeout):
        DeviceLogin(transport(api)).poll(code, sleep=sleep, clock=lambda: now[0])
    # 5 + 5 + 10 + 10 = 30 s; one more wait would pass the code's lifetime
    assert sleeps == [5, 5, 10, 10]
    assert len(api.requests) == 4


def test_device_login_understands_rfc_8628_errors(api):
    api.on(
        "POST",
        "/device/token",
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        {"status": "approved", "api_key": "nmk_new"},
    )
    sleeps = []
    assert (
        DeviceLogin(transport(api)).poll(
            DeviceCode("dc", "X", "u", 2, 60), sleep=sleeps.append
        )
        == "nmk_new"
    )
    assert sleeps == [2, 2, 7]

    api.on("POST", "/device/token", (400, {"error": "access_denied"}))
    with pytest.raises(LoginFailed, match="denied"):
        DeviceLogin(transport(api)).poll(
            DeviceCode("dc", "X", "u", 2, 60), sleep=lambda s: None
        )

    api.on(
        "POST",
        "/device/token",
        (500, {"error": {"code": "internal", "message": "oops"}}),
    )
    with pytest.raises(ApiError):
        DeviceLogin(transport(api)).poll(
            DeviceCode("dc", "X", "u", 2, 60), sleep=lambda s: None
        )
