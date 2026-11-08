# Design

This document describes how qem is built and the rules the code keeps.

## Architecture

qem has two layers.

- **Library** (`qem`, everything outside `qem.cli`): talks to the NoMac REST API,
  builds ssh command lines, moves files over ssh, and runs the SSH tunnel.
  - `transport.Transport` is the only place that does HTTP. It adds the Bearer key, the
    `Idempotency-Key` header and the `wait` long-poll parameter, and turns error envelopes
    into `errors.ApiError` subclasses.
  - `nomac.Nomac` covers the account: `me`, `credits`, `sessions`, `start`, `session`.
  - `mac_session.MacSession` is one Mac: lifecycle, jobs, `prepare_ssh`/`ssh`, `sync`/`pull`,
    and `password`/`desktop_tunnel`.
  - `models` holds typed, read-only views of responses. Each keeps the full response in
    `raw`.
  - `ssh`, `tunnel`, `upload` (sync), `transfer` (Mac paths, pull) and `vnc` (desktop) are
    free functions that `MacSession` puts together.
- **CLI** (`qem.cli`): argparse in `cli/__init__.py`, handlers in `cli/commands.py`,
  and the per-folder session store in `cli/project_store.py`.

SSH never reaches the Mac directly. ssh runs `python -m qem proxy <id>` as its
ProxyCommand, and that process carries the SSH bytes over a WebSocket to NoMac's gateway
(see [protocol.md](protocol.md)).

The desktop uses that ssh as well. An ssh port forward makes the Mac's Screen Sharing,
which listens only on the Mac's own 127.0.0.1:5900, a VNC server on this computer. It
signs in with the session's login from `/password`. The nomac.app browser page is not
used, because it needs the web login rather than the API key.

Files move as tar streams over that ssh. Neither direction uses rsync.

- **`pull`** runs `tar -c` on the Mac and unpacks the stream with Python's `tarfile`.
- **`sync`** runs a small POSIX sh script on the Mac, over one ssh connection, using only
  macOS's own tools. The exchange goes like this:
  1. The script lists the project folder with `find` and `stat`.
  2. qem compares the list with its own scan of the local folder.
  3. qem writes the paths to delete, then a tar stream of the new and changed files.
  4. The script reads the delete list with `read` and `dd bs=1`, which take no more than
     they need, deletes, and hands the rest of stdin to `tar -x`.

  Files are compared by type, size, whole-second modification time and permission bits.
  `upload.ProjectFilter` holds the skip rules. It decides for local files and for paths
  that exist only on the Mac, so `--delete` never removes what the rules leave out.
  Secrets (`upload.SECRETS`) rank above `.gitignore`, so a project's own rules cannot
  send them; only a `!` line in `.nomacignore` can. `.pem` and `.key` files count as
  secrets only when their first 64 KiB hold a private key, so certificates and Keynote
  documents still go. Names match in any case, as on Windows' and macOS's file systems.

qem does not use rsync because Windows has no rsync that just works:

- The native builds can't download from macOS's openrsync, which speaks only protocol 29.
- The MSYS2 and Cygwin builds rewrite paths when they start Windows OpenSSH.

[protocol.md](protocol.md) has the details. Without rsync there is one code path for
every platform, and it needs only ssh. What is lost is rsync's transfer of just the
changed parts of a file, which matters little for source code.

## Core principles

1. **The library never prints, prompts or calls `sys.exit`.** It returns values and
   raises `NomacError` subclasses, because other programs embed it. Output, exit codes
   and questions belong in `qem.cli`.
2. **The library keeps no hidden per-project state.** Callers pass session ids, keys and
   paths in. The map from project folder to session lives only in the CLI store, because
   a program using the library decides on its own what a "current" Mac is. The only files
   the library writes are SSH material under `~/.qem`: generated keys, pinned
   known_hosts, an empty ssh config, and the sockets ssh makes for shared connections.
   It also removes them again: `stop()` deletes the session's known_hosts and a key that
   qem generated, and a start the API refuses deletes the key made for it. A key the
   caller passed in is never deleted.
3. **Commands are never run twice by accident.** `start` and `exec` always send an
   `Idempotency-Key`. After a `NetworkError` the CLI prints the key to retry with, and the
   library has no automatic retries. The API says to reuse a key and never retry with a
   new one. A generated SSH key is named after the request key, so a retried start sends
   the same body, and the key is kept after a `NetworkError`.
4. **The tunnel's stdout carries SSH bytes and nothing else.** Every diagnostic goes to
   stderr, because ssh reads stdout as the protocol stream.
5. **Host keys are pinned, never trusted on first use.** The host key from the
   `/access` call is written to a per-session known_hosts under the alias `qem-<id>`,
   and ssh runs with `StrictHostKeyChecking=yes`. That alias is also the ssh hostname,
   which is never resolved through DNS.
6. **The access token and the Mac's password are never written to disk.** A parent that
   already called `/access` passes the token to the ProxyCommand through
   `QEM_TUNNEL_*` environment variables. On its own, `qem proxy` calls `/access` itself.
   The password reaches a VNC viewer through `VNC_USERNAME`/`VNC_PASSWORD`. `Login` keeps
   it out of its repr. The token goes only to the gateway that `/access` named, and only
   encrypted:
   - `tunnel_url` accepts only a bare `https` origin, with no login, path or query.
   - Plain `http` is accepted only for a loopback address, which the tests use.
   - The session id is percent-encoded, so it cannot change the path.
   - websockets drops the `Authorization` header when a redirect leads to another host.
7. **Unknown response fields are tolerated.** The API has no published schema
   ([protocol.md](protocol.md)). Models read fields defensively, unwrap `{"session": …}`
   and `{"job": …}` when present, and keep `raw` so callers are not blocked by a missing
   field.
8. **Separate from the npm CLI.** Everything lives under `~/.qem`, not `~/.nomac`,
   and the command is `qem`. The npm CLI's file formats are undocumented, and both
   tools may be installed side by side. The npm CLI is UNLICENSED: qem takes only
   protocol facts from it (listed in [protocol.md](protocol.md)), never its code,
   structure or wording. Names, defaults and behaviour come from NoMac's public docs, the
   API or qem's own design. Timeouts rest on measured timings. qem does not imitate the
   npm CLI where the service does not require it.

## Platform choices

- Paths inside ssh options use forward slashes on Windows. Windows OpenSSH accepts them,
  and MSYS ssh would otherwise lose the backslashes when it runs the ProxyCommand through
  `/bin/sh`.
- An empty file in `~/.qem` stands in for `/dev/null` (`-F`,
  `GlobalKnownHostsFile`), because Windows OpenSSH does not understand `/dev/null`.
- Windows OpenSSH stops writing its stdout in session 0 (ssh logins, services), so
  there qem uses Git's ssh instead, found only next to the `git` on `PATH`. The session
  comes from `ProcessIdToSessionId` through ctypes. qem replaces only Windows' own
  OpenSSH and never searches the disk; `QEM_SSH` overrides the choice.
- MSYS ssh runs the ProxyCommand through `$SHELL`, which in an ssh login to Windows is
  that server's shell, often PowerShell. For an ssh with `sh.exe` beside it,
  `prepare_ssh()` therefore sets `SHELL` to that `sh.exe` in ssh's environment.
- ControlMaster multiplexing is used only off Windows, where OpenSSH supports it.
  - Each Mac gets one socket in `~/.qem/mux`, named by a 16-digit hash of the session
    id.
  - When that path is too long for a Unix socket (macOS allows 104 bytes, and ssh adds
    17 for a temporary name), ssh runs without sharing.
  - The shared connection closes 60 s after its last use. It is kept short because an
    open connection may keep the Mac from counting as idle.
  - A port forward always gets a connection of its own. Then the port closes when that
    ssh ends, and a taken port fails it at once (`ExitOnForwardFailure`).
- ssh sends keepalives every 15 s and gives up after 8 missed answers. The provider's
  network can stall the tunnel for a while, so ssh should not give up sooner.
  `ConnectTimeout=30` limits the wait for the Mac's SSH banner. The tunnel itself
  gives up on the WebSocket handshake after 15 s.
- On Windows, a generated private key gets an ACL that lists only the current user
  (`icacls`). Windows OpenSSH ignores keys that anyone else may read, and MSYS
  `ssh-keygen` adds an OWNER RIGHTS entry that counts as that.
- `pull` unpacks with `tarfile`'s `data` filter, so an archive from the Mac cannot write
  outside the target folder.
- Windows has no executable bit, so there `sync` takes it from Git's index. Untracked
  files count as executable when they start with `#!`. Other platforms use the file's
  own permission bits.
- The project folder on the Mac has the local folder's name, unchanged apart from NFC.
  Every shell command quotes it, so no character needs replacing. Only an empty name (a
  drive root) and one over macOS's 255 bytes are refused.
- `sync` compares file names in Unicode NFC form. After `tar -x`, macOS listed a name
  decomposed (NFD), while Windows keeps it composed, so the two spellings would never
  match. Deletions use the name exactly as the Mac stores it.
