# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import io
import os
import tarfile
from pathlib import Path

import pytest

from qem import upload
from qem.errors import TransferError
from qem.upload import BEGIN, END, Entry


def make_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())  # no CRLF translation on Windows


PROJECT = {
    ".nomacignore": "# comment\n!build/keep/\nlogs/\n",
    ".gitignore": "*.tmp\nsecret/\n",
    "src/main.py": "print('hi')\n",
    "build/out.o": "obj",
    "build/keep/k.txt": "keep",
    "node_modules/x/y.js": "js",
    "logs/a.log": "log",
    "top.tmp": "tmp",
    "secret/s.txt": "s",
    "sub/.gitignore": "!keep.tmp\nlocal.txt\n",
    "sub/a.tmp": "tmp",
    "sub/keep.tmp": "kept by sub/.gitignore",
    "sub/local.txt": "local",
}


def test_scan_applies_skips_gitignores_and_nomacignore(tmp_path):
    make_tree(tmp_path, PROJECT)
    found = upload.scan(tmp_path)
    assert sorted(found.entries) == [
        ".gitignore", ".nomacignore",
        "build/keep", "build/keep/k.txt",  # forced back in; the rest of build/ stays out
        "src", "src/main.py",
        "sub", "sub/.gitignore", "sub/keep.tmp",  # the deeper .gitignore wins
    ]  # fmt: skip
    assert found.entries["src"].kind == "d"
    assert found.entries["src/main.py"].size == len("print('hi')\n")


def test_filter_decides_for_paths_that_exist_only_on_the_mac(tmp_path):
    make_tree(tmp_path, PROJECT)
    filt = upload.scan(tmp_path).filter
    assert filt.includes("src/old.py")
    assert filt.includes("build/keep/new.txt")
    assert filt.includes("sub/keep.tmp")
    for left_out in ("build/out.o", "build/", "logs/x.log", "node_modules/a/", "sub/b.tmp", "secret/", "x/secret/y"):
        assert not filt.includes(left_out), left_out


KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA==\n-----END OPENSSH PRIVATE KEY-----\n"
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_secrets_stay_local(tmp_path):
    make_tree(tmp_path, {
        ".env": "TOKEN=1", ".env.local": "TOKEN=2", ".env.example": "TOKEN=",
        "ios/AuthKey_ABC.p8": KEY, "ios/dist.P12": "p12", "android/release.jks": "jks",
        "deploy/id_ed25519": KEY, "deploy/id_ed25519.pub": "ssh-ed25519 AAAA",
        "certs/ca.pem": CERT, "certs/server.pem": CERT + KEY, "certs/server.key": KEY,
        "Slides.key": "PK\x03\x04 not a key",  # a Keynote document
        "src/main.py": "print('hi')\n",
    })  # fmt: skip
    found = upload.scan(tmp_path)
    assert sorted(p for p, e in found.entries.items() if e.kind == "f") == [
        ".env.example", "Slides.key", "certs/ca.pem", "deploy/id_ed25519.pub", "src/main.py",
    ]  # fmt: skip
    assert sorted(found.secrets) == [
        ".env", ".env.local", "android/release.jks", "certs/server.key", "certs/server.pem",
        "deploy/id_ed25519", "ios/AuthKey_ABC.p8", "ios/dist.P12",
    ]  # fmt: skip


def test_secrets_beat_gitignore_but_not_a_forced_line(tmp_path):
    make_tree(tmp_path, {".gitignore": "!.env\n", ".nomacignore": "!ios/AuthKey.p8\n", ".env": "T=1", "ios/AuthKey.p8": KEY})
    found = upload.scan(tmp_path)
    assert ".env" not in found.entries and found.secrets == [".env"]
    assert "ios/AuthKey.p8" in found.entries


def test_secrets_already_left_out_are_not_reported(tmp_path):
    make_tree(tmp_path, {".gitignore": ".env\n", ".nomacignore": "keys/\n", ".env": "T=1", "keys/a.p8": KEY})
    assert upload.scan(tmp_path).secrets == []


def test_delete_never_removes_secrets_from_the_mac(tmp_path):
    make_tree(tmp_path, {"server.key": KEY, "src/a.py": ""})
    found = upload.scan(tmp_path)
    remote = {p: Entry("f", path=p) for p in (".env", "server.key", "AuthKey.p8", "old.py", "src/a.py")}
    todo = upload.plan(found.entries, remote, found.filter, delete=True)
    assert todo.remove == ["old.py"]


def test_prune_names_keep_folders_a_forced_path_needs():
    assert upload.ProjectFilter(["!build/keep/"]).prune_names() == ["node_modules", "Pods", "DerivedData", ".build", ".git"]
    assert upload.ProjectFilter([]).prune_names() == list(upload.DEFAULT_SKIPS)
    assert upload.ProjectFilter(["!*.keep"]).prune_names() == []  # could match anywhere


def test_windows_takes_executable_bit_from_git_then_shebang(tmp_path, monkeypatch):
    make_tree(tmp_path, {"build.sh": "#!/bin/sh\n", "tool": "binary", "notes.txt": "x", "new.sh": "#!/bin/sh\n"})
    monkeypatch.setattr(upload.sys, "platform", "win32")
    monkeypatch.setattr(upload, "_git_executables", lambda root: {"build.sh": False, "tool": True, "notes.txt": False})
    modes = {path: e.mode for path, e in upload.scan(tmp_path).entries.items()}
    # Git is authoritative for tracked files, even against a shebang; untracked files go by "#!".
    assert modes == {"build.sh": 0o644, "tool": 0o755, "notes.txt": 0o644, "new.sh": 0o755}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_posix_keeps_permission_bits_and_symlinks(tmp_path):
    make_tree(tmp_path, {"run.sh": "#!/bin/sh\n"})
    (tmp_path / "run.sh").chmod(0o750)
    (tmp_path / "link").symlink_to("run.sh")
    entries = upload.scan(tmp_path).entries
    assert entries["run.sh"].mode == 0o750
    assert entries["link"] == Entry("l", len("run.sh"), entries["link"].mtime, 0o777, "run.sh")


def manifest(*lines: tuple[str, str]) -> bytes:
    names = b"".join(name.encode() + b"\0" for name, _ in lines)
    stats = "".join(line + "\n" for _, line in lines).encode()
    return b"motd noise\n" + BEGIN + names + b"\0" + stats + END


def test_parse_manifest():
    data = manifest(
        ("./a.txt", "1700000000 6 100644"),
        ("./d", "1700000001 64 40755"),
        ("./d/ü l", "1700000002 5 120755"),
    )
    assert upload.parse_manifest(data) == {
        "a.txt": Entry("f", 6, 1700000000, 0o644),
        "d": Entry("d", 64, 1700000001, 0o755),
        "d/ü l": Entry("l", 5, 1700000002, 0o755),
    }
    assert upload.parse_manifest(BEGIN + b"\0" + END) == {}


@pytest.mark.parametrize(
    "data",
    [
        b"ssh: connect failed",
        BEGIN + b"./a\0\0" + END,  # a name without its stat line
        BEGIN + b"./../x\0\0" + b"1 1 100644\n" + END,
        BEGIN + b"/etc/passwd\0\0" + b"1 1 100644\n" + END,
    ],
)
def test_parse_manifest_rejects(data):
    with pytest.raises(TransferError):
        upload.parse_manifest(data)


def test_plan_sends_changes_and_deletes_only_what_the_filter_covers():
    filt = upload.ProjectFilter(["!build/keep/"])
    same = Entry("f", 3, 100, 0o644)
    local = {
        "a": same,
        "b": Entry("f", 4, 100, 0o644),  # size changed
        "m": Entry("f", 3, 100, 0o755),  # mode changed
        "c": Entry("f", 1, 100, 0o644),  # new
        "d": Entry("d", mode=0o755),  # new folder
        "x": Entry("f", 1, 100, 0o644),  # a folder on the Mac
    }
    remote = {
        "a": same,
        "b": same,
        "m": same,
        "x": Entry("d", 64, 1, 0o755),
        "x/inner": same,
        "gone.txt": same,
        "old": Entry("d", 64, 1, 0o755),
        "old/sub": Entry("d", 64, 1, 0o755),
        "old/sub/f": same,
        "build/out.o": same,  # left out by the default skips: stays
        "build/keep/stale": same,  # forced in: removed
    }
    todo = upload.plan(local, remote, filt, delete=True)
    assert todo.upload == ["b", "m", "c", "d", "x"]
    assert todo.remove == ["x", "x/inner", "gone.txt", "old/sub/f", "build/keep/stale"]
    assert todo.rmdirs == ["old/sub", "old"]

    keep = upload.plan(local, remote, filt, delete=False)
    assert (keep.upload, keep.remove, keep.rmdirs) == (todo.upload, ["x"], [])


def test_names_compare_in_nfc_but_delete_as_stored(tmp_path):
    import unicodedata

    nfc, nfd = "Ümlaut.txt", unicodedata.normalize("NFD", "Ümlaut.txt")
    gone_nfd = unicodedata.normalize("NFD", "Ä.txt")
    make_tree(tmp_path, {nfc: "x"})
    local = upload.scan(tmp_path).entries
    st = (tmp_path / nfc).stat()
    data = manifest((f"./{nfd}", f"{int(st.st_mtime)} 1 100644"), (f"./{gone_nfd}", "1 1 100644"))
    todo = upload.plan(local, upload.parse_manifest(data), upload.ProjectFilter(), delete=True)
    assert todo.upload == []
    assert todo.remove == [gone_nfd]


def test_removal_request_format():
    todo = upload.Plan(upload=[], remove=["a b", "c"], rmdirs=["d"])
    assert upload.removal_request(todo) == b"6\na b\x00c\x002\nd\x00"
    assert upload.removal_request(upload.Plan([], [], [])) == b"0\n0\n"


def test_write_tar_round_trip(tmp_path):
    make_tree(tmp_path, {"src/main.py": "print('hi')\n"})
    entries = {
        "src": Entry("d", mode=0o755),
        "src/main.py": Entry("f", len("print('hi')\n"), 1700000000, 0o755),
        "link": Entry("l", 11, 1700000000, 0o777, "src/main.py"),
    }
    buf = io.BytesIO()
    upload.write_tar(buf, tmp_path, entries, list(entries))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert members["src"].isdir()
        assert (members["src/main.py"].mode, members["src/main.py"].mtime) == (0o755, 1700000000)
        assert tar.extractfile("src/main.py").read() == b"print('hi')\n"
        assert members["link"].issym() and members["link"].linkname == "src/main.py"


def test_remote_script_prunes_default_skips():
    script = upload.remote_script("/Users/agent/drive/src/My App", ["node_modules", "build"])
    assert "cd -- '/Users/agent/drive/src/My App'" in script
    assert "-type d \\( -name node_modules -o -name build \\) -prune -o -print0" in script
    assert upload.remote_command("/x", []).startswith("/bin/sh -c ")
