# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The `qem` command: argparse, the per-project session store and output.

Everything else is a thin call into the library.
"""

from __future__ import annotations

import argparse
import sys

from ..version_info import version

EXIT_STILL_RUNNING = 75  # EX_TEMPFAIL: the command keeps running on the Mac


def build_parser() -> argparse.ArgumentParser:
    # prog is fixed because argparse would otherwise name the program after
    # sys.argv[0], which is "__main__.py" under `python -m qem`.
    parser = argparse.ArgumentParser(
        prog="qem", description="Use NoMac cloud Macs from the terminal."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def cmd(name: str, help: str | None, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help, **kwargs)

    p = cmd("login", "link this computer to your NoMac account")
    p.add_argument(
        "--with-token", action="store_true", help="read an API key from stdin instead"
    )
    cmd("logout", "forget the stored API key")
    cmd("whoami", "show the account the API key belongs to")
    cmd("credits", "show remaining credit")

    p = cmd("start", "start a Mac for this project folder and wait until it is ready")
    p.add_argument(
        "--max-duration",
        default="2h",
        metavar="TIME",
        help="hard limit, up to 8h (default: 2h)",
    )
    p.add_argument(
        "--idle-timeout",
        default="10m",
        metavar="TIME",
        help="stop after this much idle time (default: 10m)",
    )
    p.add_argument(
        "--max-cost",
        type=float,
        default=5.0,
        metavar="USD",
        help="spend cap in dollars (default: 5)",
    )
    p.add_argument(
        "--parallel", action="store_true", help="start another Mac even if one runs"
    )
    p.add_argument(
        "--request-key", help="reuse after a lost response so no second Mac starts"
    )
    p.add_argument(
        "--wait",
        type=float,
        default=240,
        metavar="SECONDS",
        help="how long to wait for ready (default: 240)",
    )

    p = cmd("status", "show this project's Mac")
    p.add_argument("--json", action="store_true")
    p = cmd("sessions", "list the account's Macs")
    p.add_argument("--json", action="store_true")
    p = cmd("attach", "use a running Mac for this project folder")
    p.add_argument("session_id", nargs="?", help="default: the account's running Mac")
    p = cmd("extend", "add time to this project's Mac")
    p.add_argument("minutes", type=int, nargs="?", default=60)
    p = cmd("stop", "stop this project's Mac")
    p.add_argument(
        "--wait",
        type=float,
        default=90,
        metavar="SECONDS",
        help="how long to wait for stopped (default: 90)",
    )

    p = cmd(
        "exec",
        "run a command on the Mac",
        usage="%(prog)s [options] -- COMMAND [ARG ...]",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=30,
        metavar="SECONDS",
        help="wait this long, then exit 75 if still running (default: 30)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        help="kill after this long, up to 3600 (default: 300)",
    )
    p.add_argument(
        "--cwd",
        help="directory on the Mac (default: the synced project folder, if any)",
    )
    p.add_argument(
        "--request-key",
        help="reuse after a lost response so the command does not run twice",
    )
    p.add_argument("--json", action="store_true", help="print the job as JSON")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = cmd("job", "wait for a command and print its output")
    p.add_argument("job_id")
    p.add_argument("--wait", type=float, default=0, metavar="SECONDS")
    p.add_argument("--json", action="store_true")
    p = cmd("output", "read part of a command's output")
    p.add_argument("job_id")
    p.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    p.add_argument("--cursor", type=int, default=0)
    p = cmd("cancel", "stop a running command")
    p.add_argument("job_id")

    p = cmd(
        "ssh",
        "open a shell on the Mac, run a command, or forward a port",
        usage="%(prog)s [--forward LOCAL:REMOTE] [-- COMMAND [ARG ...]]",
    )
    p.add_argument(
        "--forward",
        metavar="LOCAL:REMOTE",
        help="forward a local port to a port on the Mac",
    )
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = cmd("ssh-config", "let plain `ssh NAME` reach this project's Mac")
    p.add_argument(
        "name",
        nargs="?",
        default="nomac",
        metavar="NAME",
        help="ssh host name (default: nomac)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="add the Include line to ~/.ssh/config without asking",
    )

    p = cmd("desktop", "show the Mac's desktop in a VNC viewer, through ssh")
    p.add_argument(
        "--port",
        type=int,
        metavar="PORT",
        help="local port for the viewer (default: a free one)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--viewer",
        metavar="PATH",
        help="VNC viewer to start (default: TigerVNC's vncviewer)",
    )
    group.add_argument(
        "--no-viewer",
        action="store_true",
        help="only open the tunnel and print its address",
    )
    p = cmd("password", "print the Mac's login password for this session")
    p.add_argument(
        "--copy", action="store_true", help="put it on the clipboard instead"
    )

    p = cmd("sync", "upload a project folder to ~/drive/src/<folder> on the Mac")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--delete", action="store_true", help="delete Mac files that are gone locally"
    )
    p.add_argument("--watch", action="store_true", help="keep syncing on every change")
    p = cmd("pull", "download a path from the Mac's project folder")
    p.add_argument("local", nargs="?", default=".", help="local folder (default: .)")
    p.add_argument(
        "--from",
        dest="remote",
        required=True,
        metavar="PATH",
        help="path on the Mac, relative to the project folder unless absolute",
    )
    p.add_argument(
        "--overwrite", action="store_true", help="replace files that exist locally"
    )

    # The ssh ProxyCommand; not for people.
    p = sub.add_parser("proxy")
    p.add_argument("session_id")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # Imported after parse_args so `--version` and `--help` stay fast.
    from .commands import run

    sys.exit(run(args))
