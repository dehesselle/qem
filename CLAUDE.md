# qem

A Python library and CLI for NoMac cloud Macs that replaces the npm `nomac` CLI for
sessions, jobs, ssh and sync.

- Read docs/DESIGN.md before architectural changes and respect its core principles,
  especially: the library never prints or exits, and the tunnel's stdout carries SSH
  bytes only.
- Read docs/protocol.md before touching API calls. Update it when the live service turns
  out to differ.
- docs/MANUAL.md documents every command, option and library call.

@docs/DEVELOPMENT.md

Documentation rules: the README stays short. A change to design, usage or behavior
updates the matching document in docs/ in the same change. DESIGN.md is kept current and
carries no project history.

Never run `qem start` (it costs real credit) unless the user asks for it.
