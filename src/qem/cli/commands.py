"""Command handlers. Each returns the process exit code."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config, upload, vnc
from .. import ssh as ssh_mod
from ..api import Transport
from ..auth import DeviceLogin
from ..client import Nomac
from ..errors import ApiError, NetworkError, NomacError, PaymentRequired, TransferError
from ..models import Job, SessionInfo
from ..session import MacSession, new_request_key
from . import EXIT_STILL_RUNNING
from .store import ProjectStore, find_identity


def err(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def parse_duration(text: str) -> int:
    """'90s', '45m', '2h' or a bare number of minutes, in seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text.lower())
    if not m:
        raise NomacError(f"not a duration: {text!r} (use e.g. 90s, 45m or 2h)")
    return round(float(m[1]) * {"s": 1, "m": 60, "": 60, "h": 3600}[m[2]])


def dollars(microunits: int | None) -> str:
    return "?" if microunits is None else f"${microunits / 1_000_000:.2f}"


def local_time(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def print_session(info: SessionInfo) -> None:
    print(f"{info.id}  {info.state}")
    if info.cost_microunits is not None:
        print(f"  cost so far: {dollars(info.cost_microunits)}")
    if info.idle_deadline_at:
        print(f"  stops if idle at: {local_time(info.idle_deadline_at)}")
    if info.hard_deadline_at:
        print(f"  stops at: {local_time(info.hard_deadline_at)}")
    if info.failure_code:
        print(f"  failure: {info.failure_code}")


def current(nm: Nomac, store: ProjectStore) -> MacSession:
    data = store.load()
    if not data:
        raise NomacError("no Mac for this folder: run `qem start` or `qem attach`")
    return nm.session(data["id"], identity=data.get("identity"))


# account


def cmd_login(args: argparse.Namespace) -> int:
    if args.with_token:
        key = sys.stdin.read().strip()
        if not key:
            raise NomacError("no API key on stdin")
    else:
        login = DeviceLogin(Transport())
        code = login.start()
        print(f"Open {code.url}")
        print(f"and confirm the code {code.user_code}")
        key = login.poll(code)
    me = Nomac(api_key=key).me()
    path = config.save_api_key(key)
    org = me.get("organization") or me.get("org") or {}
    name = org.get("name") if isinstance(org, dict) else None
    print(f"Logged in{f' to {name}' if name else ''}. Key stored in {path}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    print("Removed the stored API key." if config.delete_api_key() else "No stored API key.")
    if os.environ.get("NOMAC_API_KEY"):
        err("NOMAC_API_KEY is still set in the environment.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    print_json(Nomac().me())
    return 0


def cmd_credits(args: argparse.Namespace) -> int:
    print_json(Nomac().credits())
    return 0


# sessions


def cmd_start(args: argparse.Namespace) -> int:
    nm, store = Nomac(), ProjectStore()
    known = store.load()
    if known and not args.parallel:
        mac = current(nm, store)
        if mac.refresh().live:
            err(f"This folder already has a Mac: {mac.id}")
            print_session(mac.wait_ready(args.wait))
            return 0

    request_key = args.request_key or new_request_key("qem-cli")
    try:
        mac = nm.start(
            max_duration=parse_duration(args.max_duration),
            idle_timeout=parse_duration(args.idle_timeout),
            max_cost=args.max_cost,
            parallel=args.parallel,
            request_key=request_key,
            wait=None,
        )
    except NetworkError:
        err(f"No answer from NoMac. Retry with --request-key {request_key} so no second Mac starts.")
        raise
    except ApiError as e:
        if e.code != "session_limit_reached":
            raise
        running = [s.id for s in nm.live_sessions()]
        if not running:
            raise
        raise NomacError(
            f"a Mac is already running ({', '.join(running)}). `qem attach {running[0]}` uses it "
            "for this folder; `qem start --parallel` starts another one."
        ) from e

    store.save({"id": mac.id, "identity": str(mac.identity) if mac.identity else None, "request_key": request_key})
    err(f"Waiting for {mac.id} to be ready ...")
    print_session(mac.wait_ready(args.wait))
    if not mac.identity:
        err("This Mac was started elsewhere, so ssh, sync and pull will not work from here.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    info = current(Nomac(), ProjectStore()).refresh()
    print_json(info.raw) if args.json else print_session(info)
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    sessions = Nomac().sessions()
    if args.json:
        print_json([s.raw for s in sessions])
    for s in [] if args.json else sessions:
        print(f"{s.id}  {s.state:<12}  {dollars(s.cost_microunits)}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    nm, store = Nomac(), ProjectStore()
    session_id = args.session_id
    if not session_id:
        live = nm.live_sessions()
        if not live:
            raise NomacError("no running Mac: run `qem start`")
        session_id = live[0].id
    mac = nm.session(session_id, identity=find_identity(session_id))
    info = mac.refresh()
    store.save({"id": mac.id, "identity": str(mac.identity) if mac.identity else None})
    print_session(info)
    if not mac.identity:
        err("This Mac was started elsewhere, so ssh, sync and pull will not work from here.")
    return 0


def cmd_extend(args: argparse.Namespace) -> int:
    print_session(current(Nomac(), ProjectStore()).extend(args.minutes))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    store = ProjectStore()
    mac = current(Nomac(), store)
    info = mac.stop(wait=args.wait)
    print_session(info)
    if info.ended:
        store.clear()
    else:
        err("The Mac is still stopping; run `qem status` to follow it.")
    return 0


# commands


def write_stream(stream: str, data: bytes | str) -> None:
    out = sys.stdout if stream == "stdout" else sys.stderr
    out.flush()
    out.buffer.write(data.encode() if isinstance(data, str) else data)
    out.buffer.flush()


def print_rest(mac: MacSession, job_id: str, stream: str, cursor: int) -> None:
    for chunk in mac.iter_output(job_id, stream, cursor):
        write_stream(stream, chunk.data)
        if chunk.tail and chunk.tail_offset is not None and chunk.tail_offset > chunk.next_cursor:
            err(f"\n[... {chunk.tail_offset - chunk.next_cursor} bytes not kept ...]")
            write_stream(stream, chunk.tail)


def finish_job(mac: MacSession, job: Job, *, as_json: bool, waited: float) -> int:
    if as_json:
        print_json(job.raw)
    elif job.running:
        after = f" after {waited:g} s" if waited else ""
        err(f"{job.id} is still running on the Mac{after}. It was not stopped, so do not start it again.")
        err(f"Wait for it with `qem job {job.id} --wait 600`, or stop it with `qem cancel {job.id}`.")
    elif job.exited:
        for stream in ("stdout", "stderr"):
            inline = getattr(job, stream)
            if inline is None:
                print_rest(mac, job.id, stream, 0)
                continue
            write_stream(stream, inline)
            if (cursor := job.next_cursor(stream)) is not None:
                print_rest(mac, job.id, stream, cursor)
        if job.reason in ("timeout", "cancelled"):
            err(f"[{job.id} ended: {job.reason}]")
    else:
        err(f"{job.id} is {job.state}; do not rerun it. Check it with `qem job {job.id}`.")

    if job.running:
        return EXIT_STILL_RUNNING
    return job.exit_code if job.exited and job.exit_code is not None else 1


def cmd_exec(args: argparse.Namespace) -> int:
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise NomacError("nothing to run: qem exec -- COMMAND [ARG ...]")
    store = ProjectStore()
    mac = current(Nomac(), store)
    request_key = args.request_key or new_request_key("qem-cli")
    cwd = args.cwd or (store.load() or {}).get("synced_dir")
    try:
        job = mac.exec(argv, cwd=cwd, timeout=args.timeout, wait=args.wait, request_key=request_key)
    except NetworkError:
        err(f"No answer from NoMac. Retry with --request-key {request_key} so the command does not run twice.")
        raise
    return finish_job(mac, job, as_json=args.json, waited=args.wait)


def cmd_job(args: argparse.Namespace) -> int:
    mac = current(Nomac(), ProjectStore())
    job = mac.wait_job(args.job_id, args.wait) if args.wait else mac.job(args.job_id)
    return finish_job(mac, job, as_json=args.json, waited=args.wait)


def cmd_output(args: argparse.Namespace) -> int:
    mac = current(Nomac(), ProjectStore())
    chunk = mac.output(args.job_id, args.stream, args.cursor)
    write_stream("stdout", chunk.data)
    if chunk.tail and not chunk.data:
        err(f"[output before {chunk.tail_offset} was not kept; tail follows]")
        write_stream("stdout", chunk.tail)
    err(f"[next cursor: {chunk.next_cursor}{', end of output' if chunk.eof else ''}]")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    job = current(Nomac(), ProjectStore()).cancel(args.job_id)
    print(f"{job.id or args.job_id}  {job.state}" + (f"  ({job.reason})" if job.reason else ""))
    return 0


# ssh and files


def ssh_session(store: ProjectStore, *, runs_ssh: bool = True) -> MacSession:
    mac = current(Nomac(), store)
    if not mac.identity:
        raise NomacError(f"no SSH key for {mac.id}: it was started elsewhere")
    if runs_ssh:
        note_ssh_choice()
    return mac


def note_ssh_choice() -> None:
    """Say so when qem runs Git's ssh instead of the one on PATH."""
    try:
        client = ssh_mod.choose_ssh()
    except TransferError:
        return  # running ssh reports it
    if client.source == "git":
        err(f"Using Git's ssh ({client.path}): Windows OpenSSH does not finish commands without a desktop session.")


def parse_forward(text: str) -> tuple[int, int]:
    """'8080:3000' -> (8080, 3000): a local port and a port on the Mac."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*:\s*(\d{1,5})\s*", text)
    ports = (int(m[1]), int(m[2])) if m else (0, 0)
    if not all(1 <= port <= 65535 for port in ports):
        raise NomacError(f"not a port forward: {text!r} (use LOCAL:REMOTE with ports 1-65535, e.g. --forward 8080:3000)")
    return ports


def cmd_ssh(args: argparse.Namespace) -> int:
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    forward = parse_forward(args.forward) if args.forward else None
    if forward and argv:
        raise NomacError("--forward runs no command: drop the words after --")
    mac = ssh_session(ProjectStore())
    if forward:
        err(f"Forwarding http://127.0.0.1:{forward[0]} to port {forward[1]} on the Mac. Ctrl-C ends it.")
    # Like ssh itself, the words are joined for the Mac's shell.
    return mac.ssh(" ".join(argv) if argv else None, forward=forward).returncode


def ensure_include(include: str, assume_yes: bool) -> bool:
    user_config = Path.home() / ".ssh" / "config"
    text = user_config.read_text(encoding="utf-8") if user_config.exists() else ""
    if include in text.splitlines():
        return True
    if not assume_yes:
        if not sys.stdin.isatty():
            return False
        try:
            answer = input(f"Add `{include}` to the top of {user_config}? [y/N] ")
        except EOFError:  # Windows' NUL device passes isatty()
            err()
            return False
        if answer.strip().lower() not in ("y", "yes"):
            return False
    user_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Include must come before the first Host block to apply to every host.
    user_config.write_text(f"{include}\n\n{text}" if text else f"{include}\n", encoding="utf-8")
    return True


def cmd_ssh_config(args: argparse.Namespace) -> int:
    name = args.name
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise NomacError(f"not a usable ssh host name: {name!r}")
    mac = ssh_session(ProjectStore(), runs_ssh=False)
    mac.prepare_ssh()
    directory = config.home() / "ssh_config.d"
    config.write_private(directory / f"{name}.conf", ssh_mod.ssh_config_block(name, mac.id, mac.identity))
    include = f"Include {(directory / '*.conf').as_posix()}"
    if ensure_include(include, args.yes):
        print(f"`ssh {name}` now reaches {mac.id}.")
    else:
        print(f"Wrote {directory / (name + '.conf')}. Add this line to the top of ~/.ssh/config:")
        print(include)
    return 0


def copy_to_clipboard(text: str) -> None:
    """Put `text` on the system clipboard with whatever tool this platform has
    (Windows, macOS, Linux on Wayland or X11). Windows' clip.exe wants UTF-16-LE
    without a byte order mark. Raise NomacError, suggesting `qem password`
    without --copy, when no tool works.
    """
    for argv, encoding in clipboard_tools():
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            # No pipes for its output: xclip and xsel stay in the background to
            # serve the selection, and would hold a pipe open.
            result = subprocess.run([exe, *argv[1:]], input=text.encode(encoding), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return
    raise NomacError("found no working clipboard tool; plain `qem password` shows it in the terminal")


def clipboard_tools() -> list[tuple[list[str], str]]:
    """Clipboard commands to try on this platform, with the encoding each reads."""
    if sys.platform == "win32":
        return [(["clip"], "utf-16-le")]  # clip.exe reads UTF-16-LE, no byte order mark
    if sys.platform == "darwin":
        return [(["pbcopy"], "utf-8")]
    tools = [
        (["xclip", "-selection", "clipboard"], "utf-8"),
        (["xsel", "--clipboard", "--input"], "utf-8"),
        (["clip.exe"], "utf-16-le"),  # WSL reaches the Windows clipboard
    ]
    if os.environ.get("WAYLAND_DISPLAY"):
        tools.insert(0, (["wl-copy"], "utf-8"))
    return tools


def cmd_password(args: argparse.Namespace) -> int:
    login = current(Nomac(), ProjectStore()).password()
    if args.copy:
        copy_to_clipboard(login.password)
        err(f"Copied the password of {login.user} to the clipboard.")
    else:
        print(login.password)
        err(f"User: {login.user}")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    mac = ssh_session(ProjectStore())
    viewer = None if args.no_viewer else args.viewer or vnc.find_viewer()
    screen_sharing = not viewer and not args.no_viewer and sys.platform == "darwin"
    login = mac.password() if viewer or screen_sharing else None
    with mac.desktop_tunnel(args.port) as tunnel:
        address = vnc.address(tunnel.port)
        if viewer:
            err(f"Opening the Mac's desktop in {Path(viewer).stem} ({address}). Closing the viewer ends the tunnel.")
            env = {**os.environ, **vnc.viewer_env(login)}
            return subprocess.run(vnc.viewer_argv(viewer, tunnel.port), env=env, check=False).returncode
        if screen_sharing:
            subprocess.run(["open", vnc.vnc_url(login, tunnel.port)], check=True)
            err("Screen Sharing is opening. Ctrl-C ends the tunnel.")
        else:
            if not args.no_viewer:
                err("No VNC viewer found: install TigerVNC or pass --viewer.")
            print(address, flush=True)
            err(f"Connect a VNC viewer to {address} as {ssh_mod.USERNAME}; `qem password` prints the password.")
            err("Ctrl-C ends the tunnel.")
        try:
            code = tunnel.wait()
        except KeyboardInterrupt:
            return 0
        err(f"The tunnel to the Mac closed (ssh exit code {code}).")
        return 1


def _snapshot(root: Path) -> dict[str, upload.Entry]:
    try:
        return upload.scan(root, detect_executables=False).entries
    except TransferError:  # a folder vanished mid-scan; the next round sees it settled
        return {}


def watch(root: Path, action: Callable[[], None], interval: float = 1.0) -> None:
    before = _snapshot(root)
    while True:
        time.sleep(interval)
        now = _snapshot(root)
        if now != before:
            action()
            before = now


def cmd_sync(args: argparse.Namespace) -> int:
    store = ProjectStore()
    mac = ssh_session(store)
    root = Path(args.path).resolve()

    reported: set[str] = set()

    def once() -> None:
        result = mac.sync(root, delete=args.delete)
        if root == store.project:
            store.update(synced_dir=result.remote)
        removed = f", removed {result.deleted}" if result.deleted else ""
        print(f"Synced {root} -> {result.remote}: {result.uploaded} files sent{removed}", flush=True)
        if new := [p for p in result.kept_local if p not in reported]:
            err(f"Kept secrets on this computer: {', '.join(new)}. A `!` line in .nomacignore sends one.")
            reported.update(new)

    once()
    if args.watch:
        err("Watching for changes. Ctrl-C ends it.")
        try:
            watch(root, once)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    store = ProjectStore()
    mac = ssh_session(store)
    synced = (store.load() or {}).get("synced_dir")
    project = synced.rsplit("/", 1)[-1] if synced else None
    result = mac.pull(args.remote, args.local, project=project, overwrite=args.overwrite)
    kept = f", kept {result.kept} existing (use --overwrite to replace them)" if result.kept else ""
    print(f"Pulled {args.remote} -> {result.dest}: {result.written} files{kept}")
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    from ..tunnel import access_from_env, run_tunnel

    access = access_from_env(args.session_id) or Nomac().session(args.session_id).access()
    run_tunnel(access)
    return 0


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "login": cmd_login,
    "logout": cmd_logout,
    "whoami": cmd_whoami,
    "credits": cmd_credits,
    "start": cmd_start,
    "status": cmd_status,
    "sessions": cmd_sessions,
    "attach": cmd_attach,
    "extend": cmd_extend,
    "stop": cmd_stop,
    "exec": cmd_exec,
    "job": cmd_job,
    "output": cmd_output,
    "cancel": cmd_cancel,
    "ssh": cmd_ssh,
    "ssh-config": cmd_ssh_config,
    "desktop": cmd_desktop,
    "password": cmd_password,
    "sync": cmd_sync,
    "pull": cmd_pull,
    "proxy": cmd_proxy,
}


def run(args: argparse.Namespace) -> int:
    try:
        return HANDLERS[args.command](args)
    except KeyboardInterrupt:
        return 130
    except PaymentRequired as e:
        err(f"qem: {e}")
        if buy_url := (e.body.get("error") or {}).get("buy_url") or e.body.get("buy_url"):
            err(f"Buy credit: {buy_url}")
        return 1
    except NomacError as e:
        err(f"qem: {e}")
        return 255 if args.command == "proxy" else 1
