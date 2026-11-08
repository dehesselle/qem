# quick ephemeral mac

`qem` is a Python library and command-line tool for
[NoMac](https://nomac.app) cloud Macs. It can start a Mac, run commands on it, open SSH
sessions and sync a project folder. It does this without Node or npm, and it runs natively
on Windows as well as on macOS and Linux.

It covers the session part of NoMac's official `nomac` CLI: login, start/stop, exec/jobs,
ssh, sync/pull, and the Mac's desktop in a window. Every command is a thin layer over
the library, so other Python programs can do the same things without the CLI.

## Install and use

Requirements are Python 3.12+ and OpenSSH, on Windows x86_64, macOS on Apple silicon or
Linux x86_64 (the platforms that [pomona](https://pypi.org/project/pomona/), the desktop
viewer, has wheels for).

```sh
uv tool install qem
qem login          # approve the code in the browser
qem start --max-duration 30m --max-cost 1
qem sync .
qem exec -- xcodebuild -version
qem ssh
qem stop
```

From Python, after `pip install qem`:

```python
from qem import Nomac

with Nomac() as nm, nm.start(max_duration=1800, max_cost=1) as mac:
    print(mac.exec(["sw_vers"]).stdout)
```

A running Mac is billed per minute, so always pass `--max-cost` and `--max-duration`.

## Documentation

- [docs/MANUAL.md](docs/MANUAL.md): every command and option, the library API, credentials
  and local files.
- [docs/DESIGN.md](docs/DESIGN.md): architecture and core principles.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md): setup, tests and the offline tunnel check.
- [docs/protocol.md](docs/protocol.md): the NoMac API and SSH tunnel as qem uses them,
  where each fact comes from, and which parts are not yet verified against the live
  service.

## License

[0BSD](LICENSE). qem is not affiliated with NoMac.
