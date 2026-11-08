"""The library on its own: no CLI, no printing, no sys.exit."""

import sys
from pathlib import Path

import httpx
import pytest

from qem import ApiError, NetworkError, Nomac, NomacError, WaitTimeout
from qem.models import OutputChunk


@pytest.fixture(autouse=True)
def no_exit(monkeypatch):
    def forbidden(*args):
        raise AssertionError("library called sys.exit")

    monkeypatch.setattr(sys, "exit", forbidden)


@pytest.fixture
def fake_keygen(monkeypatch):
    def keygen(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE")
        Path(f"{path}.pub").write_text("ssh-ed25519 AAAA qem\n")
        return path

    monkeypatch.setattr("qem.client.generate_identity", keygen)


def test_start_exec_stop(api, make_client, fake_keygen, capsys):
    api.on("POST", "/mac/sessions", {"session": {"id": "ses_1", "state": "queued"}})
    api.on(
        "GET", "/mac/sessions/ses_1",
        {"session": {"id": "ses_1", "state": "provisioning"}},
        {"session": {"id": "ses_1", "state": "ready", "cost": {"accrued_microunits": "12000"},
                     "activity": {"hard_deadline_at": 1793364000449, "idle_deadline_at": "2026-10-30T12:30:00Z"}}},
        {"session": {"id": "ses_1", "state": "stopped"}},
    )
    api.on("POST", "/mac/sessions/ses_1/jobs", {"id": "job_1", "state": "exited", "exit_code": 0, "stdout": "14.5\n", "stderr": ""})
    api.on("DELETE", "/mac/sessions/ses_1", {"session": {"id": "ses_1", "state": "stopping"}})

    with make_client() as nm, nm.start(max_duration=1800, max_cost=1.5, request_key="rk") as mac:
        assert mac.info.ready and mac.info.cost_microunits == 12000
        assert mac.info.hard_deadline_at.isoformat() == "2026-10-30T12:40:00.449000+00:00"
        assert mac.info.idle_deadline_at.isoformat() == "2026-10-30T12:30:00+00:00"
        job = mac.exec(["sw_vers", "-productVersion"], wait=10)
        assert job.exited and job.exit_code == 0 and job.stdout == "14.5\n"

    start = api.body(0)
    assert start["limits"] == {"max_duration_seconds": 1800, "idle_timeout_seconds": 600, "spend_cap_microunits": "1500000"}
    assert start["public_key"] == "ssh-ed25519 AAAA qem"
    assert api.requests[0].headers["Idempotency-Key"] == "rk"
    exec_req = api.requests[3]
    assert exec_req.url.params["wait"] == "10" and "Idempotency-Key" in exec_req.headers
    assert api.paths()[-2:] == ["DELETE /mac/sessions/ses_1", "GET /mac/sessions/ses_1"]
    assert mac.info.state == "stopped"
    assert capsys.readouterr() == ("", "")


def test_start_without_ssh_sends_no_key(api, make_client):
    api.on("POST", "/mac/sessions", {"session": {"id": "ses_1", "state": "ready"}})
    mac = make_client().start(ssh=False)
    assert "public_key" not in api.body(0) and mac.identity is None


def test_session_limit_is_raised(api, make_client):
    api.on("POST", "/mac/sessions", (409, {"error": {"code": "session_limit_reached", "message": "A Mac runs"}}))
    with pytest.raises(ApiError) as exc:
        make_client().start(ssh=False)
    assert exc.value.code == "session_limit_reached"


def test_wait_ready_failure_and_timeout(api, make_client):
    api.on("GET", "/mac/sessions/ses_1", {"session": {"id": "ses_1", "state": "failed", "failure_code": "no_capacity"}})
    with pytest.raises(NomacError, match="no_capacity"):
        make_client().session("ses_1").wait_ready()

    ticks = iter(range(0, 1000, 100))
    nm = make_client(clock=lambda: next(ticks))
    api.on("GET", "/mac/sessions/ses_2", {"session": {"id": "ses_2", "state": "queued"}})
    with pytest.raises(WaitTimeout):
        nm.session("ses_2").wait_ready(timeout=250)


def test_long_exec_follows_job(api, make_client):
    api.on("POST", "/mac/sessions/ses_1/jobs", {"job": {"id": "job_1", "state": "running"}})
    api.on("GET", "/mac/sessions/ses_1/jobs/job_1",
           {"job": {"id": "job_1", "state": "running"}},
           {"job": {"id": "job_1", "state": "exited", "exit_code": 3}})
    job = make_client().session("ses_1").exec(["make"], wait=120)
    assert job.exit_code == 3
    # long polls of at most 50 s each, until the job exited
    assert [r.url.params["wait"] for r in api.requests] == ["50", "50", "50"]


def test_iter_output_follows_cursor(api, make_client):
    api.on("GET", "/mac/sessions/ses_1/jobs/job_1/output",
           {"data_base64": "aGVsbG8g", "next_cursor": 6},
           {"data_base64": "d29ybGQ=", "next_cursor": 11, "eof": True})
    chunks = list(make_client().session("ses_1").iter_output("job_1"))
    assert b"".join(c.data for c in chunks) == b"hello world"
    assert [r.url.params["cursor"] for r in api.requests] == ["0", "6"]


def test_output_tail():
    chunk = OutputChunk.from_json({"data_base64": "", "eof": True, "tail_base64": "ZW5k", "tail_offset": 9000}, 100)
    assert chunk.tail == b"end" and chunk.tail_offset == 9000 and chunk.next_cursor == 100


def test_extend_and_cancel(api, make_client):
    api.on("POST", "/mac/sessions/ses_1/extend", {"session": {"id": "ses_1", "state": "ready"}, "message": "ok"})
    api.on("POST", "/mac/sessions/ses_1/jobs/job_1/cancel", {"id": "job_1", "state": "exited", "reason": "cancelled"})
    mac = make_client().session("ses_1")
    assert mac.extend(30).ready
    assert api.body(0) == {"minutes": 30}
    assert mac.cancel("job_1").reason == "cancelled"


def test_password(api, make_client):
    api.on("GET", "/mac/sessions/ses_1/password",
           {"session_id": "ses_1", "user": "agent", "password": "s3cret", "variable": "NOMAC_MAC_PASSWORD"})
    login = make_client().session("ses_1").password()
    assert (login.user, login.password) == ("agent", "s3cret")


def test_prepare_ssh_pins_host_key(api, make_client, isolated_home, monkeypatch):
    monkeypatch.setattr("qem.ssh.ssh_env", dict)  # which ssh this machine has does not matter here
    api.on("POST", "/mac/sessions/ses_1/access",
           {"session_id": "ses_1", "generation": 1, "ssh": {"username": "agent", "host_key": "ssh-ed25519 HOSTKEY"},
            "gateway": "https://gw.nomac.app", "token": "t" * 40, "expires_at": "2026-10-30T13:00:00Z"})
    env = make_client().session("ses_1").prepare_ssh()
    assert env == {"QEM_TUNNEL_SESSION": "ses_1", "QEM_TUNNEL_GATEWAY": "https://gw.nomac.app",
                   "QEM_TUNNEL_TOKEN": "t" * 40}
    known = (isolated_home / "known_hosts" / "ses_1").read_text()
    assert known == "qem-ses_1 ssh-ed25519 HOSTKEY\n"


def test_prepare_ssh_adds_what_the_chosen_ssh_needs(api, make_client, isolated_home, monkeypatch):
    monkeypatch.setattr("qem.ssh.ssh_env", lambda: {"SHELL": "C:/Git/usr/bin/sh.exe"})
    api.on("POST", "/mac/sessions/ses_1/access",
           {"session_id": "ses_1", "generation": 1, "ssh": {"username": "agent", "host_key": "ssh-ed25519 HOSTKEY"},
            "gateway": "https://gw.nomac.app", "token": "t" * 40, "expires_at": "2026-10-30T13:00:00Z"})
    assert make_client().session("ses_1").prepare_ssh()["SHELL"] == "C:/Git/usr/bin/sh.exe"


def test_generated_key_lifecycle(api, make_client, fake_keygen, isolated_home):
    keys = isolated_home / "keys"

    # refused: the key can never be used, so it goes
    api.on("POST", "/mac/sessions", (409, {"error": {"code": "session_limit_reached", "message": "runs"}}))
    with pytest.raises(ApiError):
        make_client().start(request_key="rk-refused")
    assert not any(keys.iterdir())

    # no answer: the start may have happened, so the key stays, and a retry
    # with the same request key sends the same public key
    def no_answer(request):
        raise httpx.ConnectError("lost")

    flaky = Nomac(http_client=httpx.Client(transport=httpx.MockTransport(no_answer)), sleep=lambda s: None)
    with pytest.raises(NetworkError):
        flaky.start(request_key="rk-retry")
    kept = sorted(p.name for p in keys.iterdir())
    assert len(kept) == 2
    api.on("POST", "/mac/sessions", {"session": {"id": "ses_1", "state": "ready"}})
    api.on("DELETE", "/mac/sessions/ses_1", {"session": {"id": "ses_1", "state": "stopped"}})
    mac = make_client().start(request_key="rk-retry")
    assert sorted(p.name for p in keys.iterdir()) == kept
    assert mac.identity.name in kept

    # stop removes it
    mac.stop()
    assert not any(keys.iterdir())
