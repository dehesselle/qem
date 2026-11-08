# Manual

This document describes every `qem` command, the Python API, and the files and
environment variables qem uses.

## Credentials

- `qem login` prints a URL and a code. Approve it in the browser, and the API key is
  stored in `~/.qem/credentials.json`, readable only by you. The browser shows the
  computer as `qem on <hostname>`. qem waits until the code is approved or expires.
- `qem login --with-token < key.txt` stores a key created at <https://nomac.app/keys>.
- `qem logout` deletes the stored key.
- The key is looked up in this order: the `api_key` argument (library only), then
  `NOMAC_API_KEY`, then the stored key.

## Commands

A Mac belongs to the folder you ran `start` or `attach` in. Run later commands in that
folder.

### Account

| Command | What it does |
| --- | --- |
| `whoami` | Print the account (`GET /me`) as JSON. |
| `credits` | Print the remaining credit as JSON. |
| `sessions [--json]` | List the account's Macs. |

### Sessions

| Command | What it does |
| --- | --- |
| `start` | Start a Mac and wait until it is ready. If this folder already has a running Mac, use that one. If another Mac runs and `--parallel` is not set, stop with an error that names it. `attach` then uses it for this folder. |
| `status [--json]` | Show the state, the cost so far and when the Mac stops. |
| `attach [SESSION_ID]` | Use an existing Mac for this folder (default: the first running one). |
| `extend [MINUTES]` | Add time (default 60). The API allows up to 8 hours in total. |
| `stop [--wait S]` | Stop the Mac, wait until it reports `stopped` (default up to 90 s), then delete its key and known_hosts. |

Options for `start`:

- `--max-duration TIME`: hard limit, up to 8h (default `2h`).
- `--idle-timeout TIME`: stop after this much idle time (default `10m`).
- `--max-cost USD`: spend cap in dollars (default 5).
- `--parallel`: start another Mac even if one runs. It runs without the account's drive.
- `--request-key KEY`: reuse after a lost response, so no second Mac starts.
- `--wait S`: how long to wait for `ready` (default 240).

`TIME` is `90s`, `45m`, `2h`, or a bare number of minutes.

ssh, sync and pull need the SSH key the Mac was started with. A Mac started elsewhere
(the npm CLI, the web, another computer) can be used with `exec` but not with ssh.

### Running commands

```sh
qem exec [--wait 30] [--timeout S] [--cwd DIR] [--request-key KEY] [--json] -- COMMAND [ARG ...]
qem job JOB_ID [--wait S] [--json]
qem output JOB_ID [--stream stdout|stderr] [--cursor N]
qem cancel JOB_ID
```

- `exec` prints the command's stdout and stderr and exits with its exit code.
- If the command is still running after `--wait` seconds, `exec` exits **75** and prints
  the job id. Follow it with `qem job JOB_ID --wait 600`. Do not run it again: it is
  still running on the Mac.
- `--timeout` kills the command on the Mac (default 300 s, at most 3600). Use `ssh` for
  longer work.
- The working directory defaults to the folder that `sync` uploaded to, if this project
  was synced.
- If NoMac does not answer, `exec` prints a request key. Retry with `--request-key` so the
  command does not run twice.
- `job` waits (long-polling in 50 s steps) and then prints the full output. `output` reads
  one chunk from a cursor and prints the next cursor on stderr.

### SSH

```sh
qem ssh                           # interactive shell
qem ssh -- uname -a               # one command, no time limit
qem ssh --forward 8080:3000       # http://127.0.0.1:8080 reaches port 3000 on the Mac
qem ssh-config [NAME] [--yes]     # NAME defaults to nomac
```

- The words after `--` are joined with spaces for the Mac's shell, as with plain ssh.
- `--forward LOCAL:REMOTE` takes two ports from 1 to 65535 and runs no command. It fails
  at once if the local port is taken.
- The Mac user is `agent`, an administrator with passwordless sudo.
- On Linux and macOS, `ssh`, `sync` and `pull` share one connection per Mac. It closes
  60 s after the last use. Windows OpenSSH cannot share connections, so there each call
  opens its own.
- ssh sends a keepalive every 15 s and gives up after 8 missed answers, so the tunnel
  may stall for about two minutes before ssh ends.
- `ssh-config` writes `~/.qem/ssh_config.d/<NAME>.conf`. It then offers to add
  an `Include` line for that folder to the top of `~/.ssh/config` (`--yes` skips
  the question). After that, `ssh nomac`, `scp`, `rsync` and editors with remote SSH
  support reach the Mac. Give each Mac its own NAME to keep several. Run it again after
  `start` or `attach` points the folder at a new Mac, and after updating from a qem that
  wrote `qem tunnel` into the file.

### Desktop

```sh
qem desktop [--port PORT] [--viewer PATH | --no-viewer]
qem password [--copy]
```

- `desktop` shows the Mac's macOS desktop in a VNC viewer on this computer. It needs no
  browser and no sign-in at nomac.app.
- **How it connects:** an ssh port forward reaches the Mac's own Screen Sharing (port
  5900 on the Mac) through the same tunnel as `ssh`. `--port` picks the local port;
  without it, a free one is used.
- **The viewer:** by default it is TigerVNC's `vncviewer`, found on PATH or in
  `Program Files\TigerVNC`. It starts already signed in, because qem passes the
  session's login in `VNC_USERNAME` and `VNC_PASSWORD`. Closing the viewer ends the
  tunnel.
- `--viewer PATH` starts another viewer with `127.0.0.1::PORT`. It must support Apple's
  sign-in (RFB security type 30, called DH in TigerVNC). TightVNC, for example, does not.
  A viewer that ignores the two variables asks for the user (`agent`) and the password.
- On macOS without TigerVNC, Apple's Screen Sharing opens instead.
- `--no-viewer`, or no viewer found: prints `127.0.0.1::PORT` and holds the tunnel until
  Ctrl-C.
- `password` prints the Mac's login password for this session, and the user on stderr.
  `--copy` puts it on the clipboard instead (`clip`, `pbcopy`, `wl-copy`, `xclip` or
  `xsel`). The same password is in `$NOMAC_MAC_PASSWORD` on the Mac.
- By hand, the same works with `qem ssh --forward 5901:5900` and
  `vncviewer 127.0.0.1::5901`, signing in as `agent` with `qem password`.
- The Mac keeps billing while the desktop is open. Run `qem stop` when done.

### Files

```sh
qem sync [PATH] [--delete] [--watch]
qem pull [LOCAL] --from REMOTE [--overwrite]
```

- `sync` uploads the folder to `/Users/agent/drive/src/<folder name>` on the Mac, using
  the folder's name exactly as it is, spaces and all. `~/drive` is persistent storage.
  Older qem versions replaced unusual characters with `-`, so a project synced with one of
  them is sent in full once, to the new name.
- **What it sends:** only files that are new or changed. It compares size, modification
  time and permission bits, then sends whole files. It reports how many files it sent and
  removed. It needs nothing but ssh: no rsync, locally or on the Mac.
- **What it skips:**
  - `node_modules`, `Pods`, `DerivedData`, `.build`, `build` and `.git` folders
  - everything that `.gitignore` files in the project leave out
- **`.nomacignore`:** a file in the project's top folder, using `.gitignore` syntax.
  - `!pattern` lines force paths back in, even inside a skipped folder. For example,
    `!build/keep/` sends that one folder.
  - Other lines skip more paths.
- **`--delete`:** removes files and folders on the Mac that are gone locally. Paths the
  skip rules leave out are never removed, so build output the Mac made itself stays.
  Without `--delete`, nothing on the Mac is removed. The one exception is a file that
  became a folder, or the other way round, which is replaced either way.
- **Executable files and symlinks:** symbolic links arrive as links. Windows has no
  executable bit, so there a file counts as executable when Git records it as executable
  (`git update-index --chmod=+x`). A file Git doesn't track counts as executable when it
  starts with `#!`. On Linux and macOS, the file's own permission bits are used.
- `--watch` keeps syncing whenever a file changes, checking once a second.
- `pull` downloads the file or folder `REMOTE` into `LOCAL` (default `.`). For example,
  `--from build/App.ipa` writes `LOCAL/App.ipa`. A relative `REMOTE` is inside the
  project folder on the Mac, and `~/…` is the Mac's home folder.
- Files that already exist locally are kept unless you pass `--overwrite`. `pull` reports
  how many it kept.
- In Git Bash, set `MSYS_NO_PATHCONV=1` before passing an absolute Mac path such as
  `/Users/agent/…`. Otherwise Git Bash rewrites it into a Windows path before qem
  sees it.
- Windows without a desktop session (an ssh login to the Windows computer, or a
  service such as a CI runner): Windows OpenSSH stops writing its output there, so
  `sync`, `pull` and captured commands would never finish. qem then uses Git's ssh
  instead, if Git is on `PATH`, and says so. On a desktop, and with any other ssh first
  on `PATH`, nothing changes. See [Which ssh qem runs](#which-ssh-qem-runs).

### Which ssh qem runs

1. `QEM_SSH`, if set: a path or a program name on `PATH`. If it does not exist, qem
   stops with an error.
2. On Windows in session 0 (no desktop), when the `ssh` on `PATH` is Windows' own
   OpenSSH (`%SystemRoot%\System32\OpenSSH`): Git's `usr\bin\ssh.exe`, found next to
   the `git` on `PATH` (`<Git>\cmd`, `<Git>\bin` or `<Git>\mingw64\bin`). qem looks
   nowhere else.
3. `ssh` on `PATH`.

The library does the same; `qem.ssh.choose_ssh()` returns the path and the reason.

## Library

```python
from qem import Nomac, ApiError

nm = Nomac()                                   # or Nomac(api_key=..., base_url=...)
mac = nm.start(max_duration=1800, idle_timeout=600, max_cost=1.0)   # waits until ready
try:
    job = mac.exec(["xcodebuild", "-version"], wait=30)
    if job.running:
        job = mac.wait_job(job.id, timeout=600)
    print(job.exit_code, job.stdout)

    synced = mac.sync("./MyApp", delete=True)    # -> SyncResult(remote, uploaded, deleted)
    mac.ssh(["ls", "-la", synced.remote])        # remote: "/Users/agent/drive/src/MyApp"
    out = mac.ssh("uname -a", capture=True).stdout
    result = mac.pull("build/App.ipa", "./out", project="MyApp")   # -> PullResult(dest, written, kept)
finally:
    mac.stop()
```

- `Nomac.start(...)` makes a new ed25519 key under `~/.qem/keys` unless you pass
  `identity=` (a private key path whose `.pub` sits next to it). Pass `ssh=False` for a
  Mac reached only through the API. `wait=None` returns without waiting until it is ready.
- `MacSession.stop()` deletes the known_hosts entry and a key that qem generated. A
  key you passed in stays.
- `Nomac.session(id, identity=...)` gives a handle to an existing Mac.
- `Nomac.sessions()` and `Nomac.live_sessions()` list the account's Macs.
- `MacSession` is a context manager that stops the Mac on exit.
- `MacSession` methods:
  - Lifecycle: `refresh`, `wait_ready`, `extend`, `stop`.
  - Jobs: `exec`, `job`, `wait_job`, `cancel`, `output`, `iter_output`.
  - SSH and files: `access`, `prepare_ssh`, `ssh_argv`, `ssh`, `sync`, `pull`.
  - Desktop: `password()` returns a `Login(user, password, variable)`.
    `desktop_tunnel(port=None)` returns a `DesktopTunnel(port, process)` once a viewer can
    connect to `127.0.0.1::port`. Close it, or use it as a context manager.
    `qem.vnc` has `find_viewer`, `viewer_argv`, `viewer_env` and `vnc_url` to start a
    viewer.
- To run ssh yourself, merge `prepare_ssh()` into the environment of
  `subprocess.run(mac.ssh_argv(...))`. Then the ProxyCommand does not need an API key of
  its own.
- Errors all derive from `NomacError`:
  - `ApiError` carries `.code`, `.status` and `.body`. Its subclasses are
    `Unauthorized`, `PaymentRequired`, `NotFound`, `OutputExpired` and
    `PreconditionFailed`.
  - The others are `NotLoggedIn`, `NetworkError`, `LoginFailed`, `WaitTimeout`,
    `TunnelError` and `TransferError`.
- Starting while a Mac already runs raises `ApiError` with status 409 and code
  `session_limit_reached`.

## Files and environment

| Path / variable | Purpose |
| --- | --- |
| `NOMAC_API_KEY` | API key; overrides the stored one. |
| `NOMAC_API_URL` | API origin (default `https://nomac.app`). |
| `QEM_HOME` | Where qem keeps its files (default `~/.qem`). |
| `QEM_SSH` | The ssh to run, a path or a name on `PATH` (see [Which ssh qem runs](#which-ssh-qem-runs)). |
| `~/.qem/credentials.json` | Stored API key. |
| `~/.qem/keys/` | SSH keys made by `start`; deleted by `stop`. |
| `~/.qem/known_hosts/<session>` | Pinned host key of each Mac. |
| `~/.qem/projects/<hash>.json` | Which Mac belongs to which project folder (CLI). The hash is of the folder's full path. |
| `~/.qem/mux/` | Sockets of shared ssh connections (not on Windows). |
| `~/.qem/ssh_config.d/` | Host blocks written by `ssh-config`. |
| `QEM_TUNNEL_*` | Internal: hands the access token from qem to its own ProxyCommand. |
