# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""One cloud Mac: lifecycle, commands, ssh and file transfer."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Self

from . import ssh as ssh_mod
from . import transfer, upload, vnc
from .api import Transport
from .errors import NomacError, TransferError, WaitTimeout
from .models import STARTING_STATES, Access, Job, Login, OutputChunk, SessionInfo
from .tunnel import tunnel_env

MAX_WAIT = 50  # the API caps long polls at 50 s


def new_request_key(prefix: str = "qem") -> str:
    return f"{prefix}-{uuid.uuid4()}"


class MacSession:
    """Handle to a session. Use as a context manager to stop the Mac on exit."""

    def __init__(
        self,
        transport: Transport,
        session_id: str,
        *,
        identity: Path | None = None,
        info: SessionInfo | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self.id = session_id
        self.identity = Path(identity) if identity else None
        self.info = info
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:
        state = self.info.state if self.info else "?"
        return f"MacSession({self.id!r}, state={state!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def _base(self) -> str:
        return f"/mac/sessions/{self.id}"

    def _request(self, method: str, path: str = "", **kwargs: Any) -> dict[str, Any]:
        return self._transport.request(method, self._base + path, **kwargs)

    # lifecycle

    def refresh(self) -> SessionInfo:
        self.info = SessionInfo.from_json(self._request("GET"))
        return self.info

    def wait_ready(self, timeout: float = 240, interval: float = 3) -> SessionInfo:
        deadline = self._clock() + timeout
        info = self.refresh()
        while info.state in STARTING_STATES:
            if self._clock() >= deadline:
                raise WaitTimeout(
                    f"session {self.id} still {info.state} after {timeout:.0f} s"
                )
            self._sleep(interval)
            info = self.refresh()
        if not info.ready:
            detail = f" ({info.failure_code})" if info.failure_code else ""
            raise NomacError(f"session {self.id} is {info.state}{detail}")
        return info

    def extend(self, minutes: int = 60) -> SessionInfo:
        """Add time; the API allows up to 8 hours in all."""
        body = self._request("POST", "/extend", json={"minutes": minutes})
        if "session" in body:
            self.info = SessionInfo.from_json(body)
            return self.info
        return self.refresh()

    def stop(self, wait: float = 90, interval: float = 2) -> SessionInfo:
        """Stop the Mac and wait (up to `wait` s) until it reports stopped or failed."""
        body = self._request("DELETE")
        info = SessionInfo.from_json(body) if body else self.refresh()
        deadline = self._clock() + wait
        while not info.ended and self._clock() < deadline:
            self._sleep(interval)
            info = self.refresh()
        self.info = info
        if info.ended:
            ssh_mod.forget(self.id, self.identity)
        return info

    # commands

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        wait: float = 30,
        request_key: str | None = None,
    ) -> Job:
        """Run argv on the Mac and wait up to `wait` s for it to finish.

        A job that is still running comes back with `running` set: follow it
        with `wait_job(job.id)`. Retrying after a network error must reuse the
        same `request_key`, or the command runs twice.
        """
        body: dict[str, Any] = {"argv": list(argv)}
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = env
        if timeout:
            body["timeout_seconds"] = timeout
        job = Job.from_json(
            self._request(
                "POST",
                "/jobs",
                json=body,
                params={"wait": int(min(wait, MAX_WAIT))},
                idempotency_key=request_key or new_request_key(),
            )
        )
        if job.running and wait > MAX_WAIT:
            return self.wait_job(job.id, wait - MAX_WAIT)
        return job

    def job(self, job_id: str, wait: float = 0) -> Job:
        return Job.from_json(
            self._request(
                "GET", f"/jobs/{job_id}", params={"wait": int(min(wait, MAX_WAIT))}
            )
        )

    def wait_job(self, job_id: str, timeout: float) -> Job:
        """Long-poll until the job is no longer running or `timeout` s passed."""
        deadline = self._clock() + timeout
        job = self.job(job_id, wait=min(timeout, MAX_WAIT))
        while job.running and (left := deadline - self._clock()) > 0:
            job = self.job(job_id, wait=min(left, MAX_WAIT))
        return job

    def cancel(self, job_id: str) -> Job:
        return Job.from_json(self._request("POST", f"/jobs/{job_id}/cancel"))

    def output(
        self, job_id: str, stream: str = "stdout", cursor: int = 0
    ) -> OutputChunk:
        body = self._request(
            "GET", f"/jobs/{job_id}/output", params={"stream": stream, "cursor": cursor}
        )
        return OutputChunk.from_json(body, cursor)

    def iter_output(
        self, job_id: str, stream: str = "stdout", cursor: int = 0
    ) -> Iterator[OutputChunk]:
        """Read a stream from `cursor` to its current end.

        When the executor dropped the middle of a huge output, the last chunk
        carries `tail` and `tail_offset` instead of more data.
        """
        while True:
            chunk = self.output(job_id, stream, cursor)
            yield chunk
            if chunk.eof or chunk.next_cursor == cursor:
                return
            cursor = chunk.next_cursor

    # ssh and files

    def access(self) -> Access:
        return Access.from_json(self._request("POST", "/access"))

    def prepare_ssh(self) -> dict[str, str]:
        """Pin the host key and return env vars that let the ProxyCommand skip its own access call.

        Pass the result as `env` (merged with os.environ) when running
        `ssh_argv()` yourself.
        """
        access = self.access()
        if access.session_id and access.session_id != self.id:
            raise NomacError(f"access answered for {access.session_id}, not {self.id}")
        if not access.host_key:
            raise NomacError("access returned no SSH host key")
        ssh_mod.write_known_hosts(self.id, access.host_key)
        return {**tunnel_env(access), **ssh_mod.ssh_env()}

    def ssh_argv(
        self,
        command: str | Sequence[str] | None = None,
        *,
        forward: tuple[int, int] | None = None,
    ) -> list[str]:
        return ssh_mod.ssh_argv(self.id, self.identity, command, forward=forward)

    def ssh(
        self,
        command: str | Sequence[str] | None = None,
        *,
        forward: tuple[int, int] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run ssh (interactive when command is None), inheriting the terminal unless capture."""
        env = {**os.environ, **self.prepare_ssh()}
        return subprocess.run(
            self.ssh_argv(command, forward=forward),
            env=env,
            capture_output=capture,
            text=capture,
            check=False,
        )

    def password(self) -> Login:
        """The Mac's login (user agent) for this session. Screen Sharing accepts it too."""
        return Login.from_json(self._request("GET", "/password"))

    def desktop_tunnel(
        self, port: int | None = None, *, timeout: float = 20
    ) -> vnc.DesktopTunnel:
        """Forward 127.0.0.1:`port` (default: a free one) to the Mac's Screen Sharing.

        Returns once a VNC viewer can connect there, signed in with password().
        Close the tunnel (or use it as a context manager) when done.
        """
        env = {**os.environ, **self.prepare_ssh()}
        port = port or vnc.free_port()
        argv = self.ssh_argv(forward=(port, vnc.SCREEN_SHARING_PORT))
        return vnc.open_tunnel(
            argv, env, port, timeout=timeout, sleep=self._sleep, clock=self._clock
        )

    def sync(
        self, local: str | Path = ".", *, name: str | None = None, delete: bool = False
    ) -> upload.SyncResult:
        """Upload a project folder to ~/drive/src/<name> on the Mac, sending only what changed.

        With `delete`, files on the Mac that are gone locally are removed too,
        except paths the project's ignore rules leave out.
        """
        project = Path(local).resolve()
        if not project.is_dir():
            raise TransferError(f"{project} is not a folder")
        remote = transfer.remote_dir(name or transfer.remote_name(project))
        found = upload.scan(project)
        env = {**os.environ, **self.prepare_ssh()}
        argv = self.ssh_argv(upload.remote_command(remote, found.filter.prune_names()))
        return upload.run(argv, env, project, remote, found, delete=delete)

    def pull(
        self,
        remote: str,
        local: str | Path = ".",
        *,
        project: str | None = None,
        overwrite: bool = False,
    ) -> transfer.PullResult:
        """Download a Mac file or folder into a local folder, keeping existing files unless overwrite.

        A relative `remote` is inside the project folder `project` (default:
        the name `sync` would use for the current directory).
        """
        dest = Path(local).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        source = transfer.resolve_remote(
            remote, project or transfer.remote_name(Path.cwd())
        )
        env = {**os.environ, **self.prepare_ssh()}
        argv = self.ssh_argv(transfer.pull_command(source))
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            try:
                written, kept = transfer.extract(proc.stdout, dest, overwrite=overwrite)
                failure = None
            except tarfile.TarError as e:
                failure = e
            finally:
                proc.stdout.close()
            returncode = proc.wait()
            errors.seek(0)
            detail = errors.read().decode(errors="replace").strip()
        if returncode != 0:
            raise TransferError(
                f"pull of {source} failed (exit code {returncode})"
                + (f": {detail}" if detail else "")
            )
        if failure:
            raise TransferError(f"pull of {source} failed: {failure}")
        return transfer.PullResult(dest, written, kept)
