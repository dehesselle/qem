# Development

This document covers setting up, testing and versioning qem.

## Setup

```sh
uv sync                     # creates .venv, installs qem editable plus pytest
uv run qem --help
```

The version comes from git tags through hatch-vcs and is written to the generated
`src/qem/version.py`. Tag releases as `vX.Y.Z`. After a new commit or tag, run
`uv sync --reinstall-package qem` to refresh that file.

## Tests

```sh
uv run pytest
uvx ruff check src tests    # ruff's default rules; the generated version.py is excluded
```

- None of the tests touch the real NoMac service. `QEM_HOME` points at a temporary
  folder and `NOMAC_API_KEY` is a dummy.
- `tests/conftest.py` has `FakeApi`, a route table for `httpx.MockTransport`. Library tests
  build `Nomac(http_client=…, sleep=…)` with it.
- `test_library.py` runs the library on its own and fails if anything prints or calls
  `sys.exit` (core principle 1 in [DESIGN.md](DESIGN.md)).
- `test_cli.py` calls `qem.cli.main()` with `Nomac` patched to use the fake API, and
  checks exit codes (including 75) and output.
- `test_upload.py` covers `sync` without a Mac: the skip rules, the local scan, the
  parsing of the Mac's file list, what gets sent and removed, and the tar stream. The
  Mac-side shell script is checked only against the live service.
- `test_vnc.py` covers the desktop without a Mac. A small Python process that opens the
  port stands in for ssh, and the test checks that the tunnel waits for the port, reports
  ssh's errors, and is closed afterwards.
- `test_tunnel.py` starts a local WebSocket server and runs `python -m qem proxy`
  as a real subprocess. It checks that every byte value passes through unchanged, that
  the Bearer header reaches the server, and that a refused handshake ends with exit code
  255 and nothing on stdout. It also pins which gateway URLs `tunnel_url` refuses.
- `test_ssh_transfer.py` checks the ssh command lines: host-key pinning, batch mode,
  multiplexing and forwards. It also runs the real `ssh-keygen` when there is one.
- `test_ssh_choice.py` covers which ssh qem runs (`QEM_SSH`, Git's ssh without a desktop
  on Windows), on a fake Windows layout.
- `test_api.py` covers the device login, including RFC 8628's `slow_down` and the
  login's expiry.

## Checking ssh without a NoMac account

The unit tests do not cover a real ssh client running the ProxyCommand. To check that
part, run a local WebSocket server that bridges to any public SSH server (for example
`github.com:22`). Then set `QEM_TUNNEL_SESSION`, `QEM_TUNNEL_GATEWAY`
(`http://127.0.0.1:<port>`) and `QEM_TUNNEL_TOKEN`, write that server's host key with
`qem.ssh.write_known_hosts`, and run `qem.ssh.ssh_argv(...)`.

ssh should get as far as `Permission denied (publickey)`. That means the ProxyCommand,
the tunnel and host-key pinning work. Try it with Windows OpenSSH (from PowerShell) and
with Git's MSYS ssh (from Git Bash).

On a Linux devbox, bridge to a throwaway `sshd -D -f <config>` on another port, with its
own host key and an `AuthorizedKeysFile` holding a key from `generate_identity`. Set
`ssh.USERNAME` to a user that exists there. Root needs `UsePAM yes` if its password is
locked. With a key that is accepted, this also checks commands, binary stdin,
connection sharing and `--forward`.

On a Windows devbox, bridge to a Unix sshd (a Linux devbox's port 22 works), not to the
box's own OpenSSH Server: its PowerShell login shell mangles binary stdin. Inside an ssh
session (session 0), Windows OpenSSH 9.5 stops writing its stdout, even without qem, so
qem picks Git's ssh there. To check Windows OpenSSH itself, run the check in a signed-in
desktop session, for example as a scheduled task with `schtasks /IT`, or set `QEM_SSH`
to it and expect the hang.

## Against the live service

The end-to-end check uses real credit. Cap it:

```sh
qem start --max-duration 30m --max-cost 1
qem exec -- sw_vers
qem exec --wait 5 -- sleep 20; qem job <JOB_ID> --wait 60
qem ssh -- uname -a
qem sync . && qem sync . && qem exec -- ls   # the second sync sends 0 files
qem pull ./tmp-pull --from README.md
qem password --copy
qem desktop                 # TigerVNC opens signed in; closing it ends the command
qem stop
```

Record what the service actually returned in [protocol.md](protocol.md).

On Windows, run this in PowerShell (Windows OpenSSH) and in Git Bash (MSYS ssh). In Git
Bash, set `MSYS_NO_PATHCONV=1` for absolute Mac paths.
