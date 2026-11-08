# NoMac protocol notes

This document describes the parts of the NoMac API and the SSH tunnel that qem uses.
Everything below was checked against the live service on 2026-10-30, unless a note says
otherwise.

## Sources

Each fact below comes from one of three sources. A fact that was read in the npm package
first and confirmed live later is listed under the package.

- **NoMac's public docs**: <https://nomac.app/docs/api>, <https://nomac.app/docs/cli>,
  the other pages under <https://nomac.app/docs> and <https://nomac.app/llms.txt>.
  - The API: the base URL, auth, the error format, `Idempotency-Key`, long polls, `/me`,
    credits, and starting, listing and stopping sessions with their limits.
  - Session states and fields: the `stopping` state, `end_reason` and `stop_notice`.
  - Jobs: their output, `stdout_next_cursor`, and the 64 KiB and 8 MiB output limits.
  - Access: `/password` and `$NOMAC_MAC_PASSWORD`; `/access`, which gives a short-lived
    gateway token and the Mac's host key; the basic device login; `session_limit_reached`.
  - The CLI's commands and options: extend (60 minutes by default, 8 hours in total),
    cancel, `output`, exit code 75, the ssh host name `nomac`, `desktop --app` opening
    Apple's Screen Sharing, `pull --overwrite`.
  - Sync: `~/drive/src/<folder>` and the skip rules, including `.nomacignore` and
    secret files kept local.
- **The published `@nomac/cli` package** (0.20.0, and 0.21.0 for the desktop). It was
  read for what the public docs leave out. The package is UNLICENSED, so only facts
  about the protocol are taken from it, never code, structure, names or wording. qem's
  code is written from the facts in this document. Read in the package:
  - the SSH tunnel: the WebSocket path, the Bearer token from `/access`, raw SSH bytes
    with no handshake, no compression, frames of at most 1 MiB, close code 1000, no
    resume
  - the field names of the `/access` answer
  - extend: the REST path, `{minutes}`, the range 1 to 480 and the answer
    `{session, message}`
  - the REST path for cancelling a job
  - the device login: the `client_name` field, `verification_uri_complete`, `expires_in`,
    the token statuses and polling at `max(2, interval)`
  - starting a Mac: `profile: "standard"`, the `limits:{…}` object, the spend cap as an
    integer string
  - session fields: `cost.accrued_microunits`, `activity.idle_deadline_at` and
    `hard_deadline_at`, `failure_code`, `drive`, `image`
  - job fields: the states running, exited and unknown, `reason` (timeout, cancelled),
    `signal`; for output, `eof` and `total_bytes`
  - the desktop: an ssh forward to port 5900, signed in with the `/password` login, and
    `vnc://user:password@…` for Apple's Screen Sharing
- **Our own tests** of the live service and the Mac:
  - `stdout_more` and `output_truncated` in the job object, first seen on 2026-10-30
  - deadlines as epoch milliseconds
  - the Mac itself, timings, Screen Sharing's RFB details and the rsync tests (below)

NoMac publishes no OpenAPI spec: `/openapi.json` and `/api/v1/openapi.json` both answer
404. The PyPI package `nomac` covers only the Simulator API.

## Basics

- Base URL: `https://nomac.app/api/v1`. Auth: `Authorization: Bearer nmk_…`.
- Errors: `{"error": {"code", "message"}}`. 402 means no credit and may carry a
  `buy_url`. 410 `output_expired` means old logs were removed. 422 means a failed
  precondition.
- `Idempotency-Key`: if the same key arrives with the same body, the API returns the
  original operation.
- Long polls: `?wait=0..50` seconds.

## Endpoints used

| Call | Notes |
| --- | --- |
| `POST /device/start {client_name}` | → `device_code, user_code, verification_uri[_complete], interval, expires_in` |
| `POST /device/token {device_code}` | → `status`: pending, slow_down, approved (+`api_key`), denied, expired, claimed |
| `GET /me` | `{org:{id, name, tier, created_at}, auth:{via, key_id}, projects, connections}` |
| `GET /mac/credits` | `{credits:{spendable_microunits, spendable_seconds, concurrency_limit, pricing, …}, purchases, low_credit}`. Amounts are integer strings in microdollars. |
| `POST /mac/sessions` | Body `{profile:"standard", public_key, limits:{max_duration_seconds, idle_timeout_seconds, spend_cap_microunits}, parallel?}`, with `spend_cap_microunits` as an integer string. While a Mac runs: 409 `session_limit_reached`, with the running id only in the message. |
| `GET /mac/sessions` | `{sessions:[…], limit}` |
| `GET /mac/sessions/:id` | `{session, image}`. `session.state`: queued, provisioning, ready, stopping, stopped, failed. Also `cost.accrued_microunits`, `activity.idle_deadline_at`/`hard_deadline_at` (epoch milliseconds or null), `failure_code`, `end_reason`, `drive`. |
| `POST /mac/sessions/:id/extend {minutes}` | 1 to 480; answers `{session, message}`. |
| `DELETE /mac/sessions/:id` | Then poll until stopped or failed. |
| `POST /mac/sessions/:id/jobs?wait=` | Body `{argv, cwd?, env?, timeout_seconds?}`. A flat job object: `id`, `state` (running, exited, unknown), `exit_code` (null when killed), `signal`, `reason` (timeout, cancelled), `output_bytes`, `output_truncated`, and `stdout`/`stderr` (first 64 KiB). When a stream was cut off, `stdout_more` and `stdout_next_cursor` (likewise for stderr). |
| `GET /mac/sessions/:id/jobs/:job?wait=` | Same job object. |
| `POST /mac/sessions/:id/jobs/:job/cancel` | Same job object. |
| `GET …/jobs/:job/output?stream=&cursor=` | `data_base64, next_cursor, eof, tail_base64, tail_offset, total_bytes`. |
| `POST /mac/sessions/:id/access` | `{session_id, generation, ssh:{username:"agent", host_key}, gateway, token, expires_at}` |
| `GET /mac/sessions/:id/password` | `{session_id, user:"agent", password, variable:"NOMAC_MAC_PASSWORD"}`. The Mac's login for this session; seen with 24 alphanumeric characters. |

## SSH tunnel

- WebSocket to `wss://<gateway>/api/v1/mac/sessions/<id>/ssh` with
  `Authorization: Bearer <token from /access>`.
- No subprotocol, no compression, frames of at most 1 MiB.
- The connection carries raw SSH bytes and has no handshake of its own. A normal end is
  close code 1000, and a broken connection cannot be resumed.
- The ssh user is `agent`, and the host key to pin is the one `/access` returns. The
  ssh host name never reaches the gateway, so any local name works.
- Synced projects live in `/Users/agent/drive/src/<folder>`.

## Desktop

`@nomac/cli@0.21.0` has two ways to show the desktop:

- `nomac desktop` opens `https://nomac.app/mac/desktop/<id>`. That page signs in with the
  nomac.app web login, not the API key, so qem cannot use it.
- `nomac desktop --app` works on macOS only. It forwards a local port over ssh to
  `127.0.0.1:5900` on the Mac and opens `vnc://user:password@…` in Apple's Screen Sharing,
  with the login from `/password`.

qem forwards the same port and shows it with pomona, which speaks Standard mode with
Adaptive quality (security type 30). Observed on 2026-10-30, with TigerVNC as the
viewer at the time:

- Port 5900 on the Mac is macOS Screen Sharing. It answered `RFB 003.889` and offered
  security types 30, 33, 36 and 35. Type 2 (a plain VNC password) was not offered.
- TigerVNC 1.16.2 on Windows picked type 30 (its "DH") with its default settings. It
  signed in from `VNC_USERNAME`/`VNC_PASSWORD` without a prompt, and the window was
  titled "NoMac - TigerVNC". The stream ran at about 16 Mbit/s through the tunnel.
- The forward worked through Windows OpenSSH and through Git's MSYS ssh.
- `activity.idle_deadline_at` was null and `activity.state` was `unknown`, with and
  without a forward open. Whether an open VNC connection holds off the idle timeout
  could not be read from the API and was not checked.

## The Mac

Observed on 2026-10-30:

- The Mac runs macOS 26.6.2 on Apple silicon. The user is `agent`, with `/bin/zsh`, and
  the default working directory is `/Users/agent/workspace`.
- `~/drive` is the account's persistent drive (20 GiB). It is saved as a new version at
  every stop, and it is deleted 90 days after its last use.
- A Mac was ready 36 to 47 s after the start call, and stopped 9 to 15 s after the stop
  call.
- The minimum charge is 1 minute, billed in 1-minute steps. Credit is reserved for the
  time ahead and returned if unused.
- A `--parallel` Mac runs without the account's drive.
- `sync` uses only base macOS tools: `/bin/sh`, `find`, `xargs`, BSD `stat -f`, `dd`,
  `mktemp` and bsdtar.
- Names that `tar -x` wrote came back from `find` in decomposed Unicode (NFD), for
  example `Ü` as `U` plus a combining mark.
- `sync` timings, from Windows through the tunnel:
  - a run with nothing to send: 2.7 to 3.2 s, also with 2,000 files in the project
  - 2,000 small files plus 20 MiB: 8 to 11 s

## rsync on the Mac

The Mac's `/usr/bin/rsync` is openrsync (protocol 29, "rsync version 2.6.9 compatible").

Pushing to it worked every time with both Windows clients tried: oc-rsync 0.6.4
(a Rust rsync) and a native Windows build of upstream rsync 3.5.0 (protocol 32). The
upload covered new, changed and deleted files, `--delete`, `--checksum`, and a run with
nothing to do. It worked through Windows OpenSSH and through Git's MSYS ssh, and printed no
warnings. The native 3.5.0 build has no symlink support and refuses `-l`.

Pulling from it failed with both Windows clients, for different reasons:

- With oc-rsync 0.6.4, the remote openrsync sender exited with code 14 at the end of most
  transfers: always when there was nothing to transfer, sometimes otherwise. It printed
  nothing on stderr.
- With the native Windows build of rsync 3.5.0 (`3.5.0-gdeeda96f`), every pull ended with
  code 12, "connection unexpectedly closed", after the files had arrived.

The code 12 comes from the Windows build itself. Neither openrsync nor the tunnel causes
it. The tests behind this:

- Upstream rsync 3.5.0, built on the Mac, pulled from openrsync without errors. That
  covered a folder, a single file, a run with nothing to do, and `--protocol=29`.
- openrsync sent the Windows client and the Mac client the same 446 bytes and exited 0
  both times.
- ssh, Windows OpenSSH as well as MSYS ssh, handed every one of those bytes to the Windows
  rsync and exited 0.
- The Windows build also failed against upstream rsync 3.5.0 on the Mac once forced to
  `--protocol=29`. At protocol 32 it succeeded.

So the Windows build fails as a receiver at protocol 29, and openrsync speaks only
protocol 29.

MSYS2's rsync 3.5.1 (msys2-runtime 3.6.11) can pull from openrsync. It was tested on a
separate Windows Server 2025 machine:

- Pulls exited 0 with MSYS2's ssh and with Windows OpenSSH. That covered a folder, a single
  file, a run with nothing to do, `-c` and `--protocol=29`.
- Pushes with `-az` exited 0, and a symlink arrived on the Mac as a symlink.
- With Windows OpenSSH, `MSYS2_ARG_CONV_EXCL='*'` was set. Without it, MSYS2 rewrites
  POSIX paths in the arguments it passes to Windows programs.
- Creating symlinks from a pull depends on the `MSYS` variable. rsync creates a link
  before its target exists.
  - `winsymlinks:native` and `winsymlinks:sys` exited 0 and created links.
  - With `MSYS` unset (deep copy) or set to `winsymlinks:nativestrict`, the link failed
    with code 23 on the first run.

For these reasons qem uses no rsync. `sync` and `pull` both stream tar over ssh
([DESIGN.md](DESIGN.md)).
