"""`build.py deps` and `build.py scan`: vendoring, the classifier, coverage checks, refresh.

Ports the tests of the two tools this merged (a vendoring tool with a SHA-pinned deps branch and a
mirror tool), then adds cases for what the merge keeps: git packages, `--ref`, the scan contract.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from conftest import commit_all, git, init_repo

GIT_SOURCE = "git+https://example.test/org/lib.git?rev=abc123#abc123"
LOCK = f"""version = 4

[[package]]
name = "app"
version = "0.1.0"

[[package]]
name = "dep"
version = "1.2.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{'a' * 64}"

[[package]]
name = "lib"
version = "0.0.1"
source = "{GIT_SOURCE}"
"""
LIB_HEAD = "# lib at abc123, with what it inherits written out."


def crate_archive(prefix: str, files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path, body in files.items():
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def write_lock(build, directory: Path, text: str = LOCK):
    path = directory / "Cargo.lock"
    path.write_text(text, encoding="utf-8")
    return build.read_lock(path)


def vendored(build, crates: Path, name: str, checksum, head: str = "[package]", extra: dict | None = None) -> None:
    folder = crates / name
    folder.mkdir(parents=True)
    (folder / "Cargo.toml").write_bytes((head + "\n").encode())
    files = {"Cargo.toml": build.sha256((head + "\n").encode())}
    for rel, body in (extra or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(body)
        files[rel] = build.sha256(body)
    (folder / build.CHECKSUM).write_text(json.dumps({"files": files, "package": checksum}), encoding="utf-8")


def ctx_for(build, root: Path, cfg_text: str = ""):
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "build.cfg").write_text(cfg_text, encoding="utf-8")
    return build.Ctx(root)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.test",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUILD_DEPS", raising=False)
    monkeypatch.delenv("BUILD_CRATES_URL", raising=False)


# --- coverage -------------------------------------------------------------------------------------


@pytest.fixture
def covered(build, tmp_path):
    lock = write_lock(build, tmp_path)
    crates = tmp_path / "deps" / "crates"
    vendored(build, crates, "dep-1.2.3", "a" * 64)
    vendored(build, crates, "lib-0.0.1", None, head=LIB_HEAD)
    return lock, crates


def test_a_vendor_directory_that_matches_the_lock_has_no_problems(build, covered):
    lock, crates = covered
    assert build.problems(lock, build.read_dir(crates)) == []
    assert build.problems(lock, build.read_dir(crates, False), False) == []


def test_a_missing_package_is_named(build, covered):
    lock, crates = covered
    shutil.rmtree(crates / "dep-1.2.3")
    assert build.problems(lock, build.read_dir(crates)) == ["crates/dep-1.2.3: missing, Cargo.lock names it"]


def test_a_changed_package_is_named_by_both_checks(build, covered):
    lock, crates = covered
    (crates / "dep-1.2.3" / build.CHECKSUM).write_text(json.dumps({"files": {}, "package": "b" * 64}))
    assert any("package checksum" in line for line in build.problems(lock, build.read_dir(crates, False), False))
    assert any("package checksum" in line for line in build.problems(lock, build.read_dir(crates)))


def test_an_edited_file_is_found_by_the_deep_check_only(build, covered):
    lock, crates = covered
    (crates / "dep-1.2.3" / "Cargo.toml").write_bytes(b"[package] # edited\n")
    assert build.problems(lock, build.read_dir(crates, False), False) == []
    assert any("SHA-256" in line for line in build.problems(lock, build.read_dir(crates)))


def test_a_git_package_at_another_commit_is_named(build, covered):
    lock, crates = covered
    (crates / "lib-0.0.1" / "Cargo.toml").write_bytes(b"# lib at def456, with what it inherits written out.\n")
    assert any("not lib at abc123" in line for line in build.problems(lock, build.read_dir(crates, False), False))


def test_a_stale_package_and_a_loose_file_are_named(build, covered):
    lock, crates = covered
    vendored(build, crates, "gone-9.9.9", "c" * 64)
    (crates / "README.md").write_text("x")
    found = build.problems(lock, build.read_dir(crates))
    assert "crates/gone-9.9.9: stale, Cargo.lock does not name it" in found
    assert any("README.md: a file" in line for line in found)


def test_a_binary_left_in_a_package_is_named(build, tmp_path):
    crates = tmp_path / "deps" / "crates"
    vendored(build, crates, "dep-1.2.4", "d" * 64, extra={"data.bin": bytes(range(256)) * 8})
    lock = write_lock(build, tmp_path, LOCK.replace("1.2.3", "1.2.4").replace("a" * 64, "d" * 64).replace(GIT_SOURCE, "").replace('source = ""', ""))
    found = build.problems(lock, build.read_dir(crates))
    assert any("data.bin" in line for line in found), found


def test_a_file_listed_in_the_checksum_but_missing_and_one_unlisted_are_named(build, covered):
    lock, crates = covered
    (crates / "dep-1.2.3" / "extra.rs").write_text("x")
    sums = json.loads((crates / "dep-1.2.3" / build.CHECKSUM).read_text())
    sums["files"]["gone.rs"] = "0" * 64
    (crates / "dep-1.2.3" / build.CHECKSUM).write_text(json.dumps(sums))
    found = build.problems(lock, build.read_dir(crates))
    assert any("gone.rs: listed in .cargo-checksum.json, missing" in line for line in found)
    assert any("extra.rs: not listed in .cargo-checksum.json" in line for line in found)


def test_a_package_taken_from_two_sources_is_refused(build, tmp_path):
    two = LOCK + f'\n[[package]]\nname = "dep"\nversion = "1.2.3"\nsource = "{GIT_SOURCE}"\n'
    with pytest.raises(build.Problem, match="more than one source"):
        build.wanted_packages(write_lock(build, tmp_path, two))


def test_a_lock_source_that_is_neither_crates_io_nor_git_is_refused(build, tmp_path):
    other = LOCK.replace("registry+https://github.com/rust-lang/crates.io-index", "registry+https://elsewhere.test/index")
    with pytest.raises(build.Problem, match="only crates.io and git"):
        write_lock(build, tmp_path, other)


# --- the classifier ---------------------------------------------------------------------------------

PEM = b"-----BEGIN PRIVATE KEY-----\n" + b"QUJDREVGR0hJSktMTU5PUA==\n" * 3 + b"-----END PRIVATE KEY-----\n"


def noise(n: int) -> bytes:
    out, block = b"", b"seed"
    while len(out) < n:
        block = hashlib.sha256(block).digest()
        out += block
    return out[:n]


FLAGGED = {
    "zip": b"PK\x03\x04" + b"\0" * 30,
    "gzip": b"\x1f\x8b\x08" + b"\0" * 30,
    "bzip2": b"BZh9" + b"\0" * 30,
    "xz": b"\xfd7zXZ\x00" + b"\0" * 30,
    "7z": b"7z\xbc\xaf\x27\x1c" + b"\0" * 30,
    "tar": b"\0" * 257 + b"ustar" + b"\0" * 300,
    "windows executable": b"MZ" + b"\0" * 62,
    "elf executable": b"\x7fELF" + b"\0" * 60,
    "mach-o executable": b"\xcf\xfa\xed\xfe" + b"\0" * 60,
    "git pack": b"PACK\0\0\0\x02",
    "der (certificate or key)": b"\x30\x82\x01\x0a" + b"\0" * 20,
    "openssl-encrypted": b"Salted__" + b"\0" * 20,
    "wasm": b"\x00asm\x01\x00\x00\x00",
    "pdf": b"%PDF-1.7\n",
    "sqlite": b"SQLite format 3\x00" + b"\0" * 80,
    "private key in text": PEM,
    "opaque high-entropy data": noise(70000),
    "unrecognised binary": b"\xff\xfe" * 200,
}
PASSING = {
    "text": b"fn main() {}\n",
    "empty": b"",
    "png": b"\x89PNG\r\n\x1a\n" + b"\0" * 20,
    "jpeg": b"\xff\xd8\xff\xe0" + b"\0" * 20,
    "font": b"OTTO" + b"\xff" * 40,
    "heic": b"\0\0\0\x18ftypheic" + b"\0" * 20,
    "a pem label quoted in docs": b"Use -----BEGIN PRIVATE KEY----- headers in tests.\n",
}


@pytest.mark.parametrize("kind", sorted(FLAGGED))
def test_every_refused_kind_is_named(build, kind):
    assert build.flagged_kind(FLAGGED[kind], "file.dat") == kind


@pytest.mark.parametrize("label", sorted(PASSING))
def test_text_and_images_pass(build, label):
    assert build.flagged_kind(PASSING[label], "file.dat") is None


# Markdown with a UTF-8 "i" and one stray ISO-8859-1 byte, the profile of three benchmarks/README.md
# blobs in a real history: 6.7 KB, 3 bytes from 0x80 up, no control character.
MIXED_MARKDOWN = (
    b"# Benchmarks\n\n" + b"| case | median | note |\n|---|---|---|\n" * 120
    + "Bohío's code at `1248e0`\n".encode() + b"Boh\xedo is the host.\n"
)
# Spanish prose in ISO-8859-1: about 4% of its bytes are from 0x80 up (ordinary prose is 2 to 5%).
LATIN1_PROSE = ("La canción del señor niño, mañana también llega, y los niños cantan en la plaza del pueblo. " * 80).encode("latin-1")
ELF_WITH_STRINGS = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"/lib64/ld-linux.so.2\x00printf\x00" * 40


@pytest.mark.parametrize("body", [MIXED_MARKDOWN, LATIN1_PROSE], ids=["mixed", "latin1"])
def test_text_in_a_single_byte_encoding_passes_with_a_note(build, body):
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")
    assert build.flagged_kind(body, "README.md") is None
    assert build.text_note(body) == "text, not UTF-8"


def test_text_note_is_only_for_text_that_is_not_utf8(build):
    assert build.text_note(b"plain ascii\n") is None
    assert build.text_note("Bohío\n".encode()) is None
    assert build.text_note(b"\x00" * 40) is None


@pytest.mark.parametrize("label", ["pem after latin-1 text", "pem in latin-1 prose"])
def test_a_private_key_in_latin1_text_is_still_refused(build, label):
    body = b"caf\xe9 key:\n" + PEM if label.startswith("pem after") else LATIN1_PROSE + PEM + LATIN1_PROSE
    assert build.flagged_kind(body, "notes.txt") == "private key in text"


@pytest.mark.parametrize(
    "label, body",
    [
        ("random bytes", noise(4096)),
        ("elf header with strings", ELF_WITH_STRINGS),
        ("pe header", b"MZ\x90\x00\x03" + b"\x00" * 55 + b"This program cannot be run in DOS mode.\r\r\n$" + b"\x00" * 200),
        ("text with a NUL", b"printable text\n" * 400 + b"\xff\x00"),
        ("text with a control character", b"printable text\n" * 400 + b"\xff\x01"),
        ("mostly high bytes", b"\xe9\xe8" * 300 + b"abc"),
        ("utf-16 text", "a readable line of text\n".encode("utf-16") * 30),
    ],
    ids=lambda v: v if isinstance(v, str) else "body",
)
def test_binary_content_is_not_mistaken_for_legacy_text(build, label, body):
    # A NUL or control byte in otherwise valid UTF-8 is text to a scanner too: only non-UTF-8 is judged here.
    assert build.flagged_kind(body, "file.dat") in {"unrecognised binary", "opaque high-entropy data", "elf executable", "windows executable"}
    assert build.text_note(body) is None


def test_a_key_file_is_refused_by_its_name(build):
    assert build.flagged_kind(b"text", "server.pfx") == "key or keystore file"


def test_allow_lets_one_kind_through_one_glob_only(build):
    allow = build.Allow(["cdn/www.gstatic.com/flutter-canvaskit/*:wasm"])
    assert allow.permits("cdn/www.gstatic.com/flutter-canvaskit/abc/canvaskit.wasm", "wasm")
    assert not allow.permits("cdn/www.gstatic.com/flutter-canvaskit/abc/canvaskit.wasm", "elf executable")
    assert not allow.permits("crates/x/a.wasm", "wasm")
    with pytest.raises(build.Problem, match="GLOB:KIND"):
        build.Allow(["nonsense"])


# --- what is written for a package -------------------------------------------------------------------


def test_tests_are_dropped_and_a_binary_a_source_includes_becomes_text(build, tmp_path):
    files = {
        "Cargo.toml": b'[package]\nname = "dep"\n\n[[test]]\nname = "t"\n\n[lib]\npath = "src/lib.rs"\n',
        "src/lib.rs": b'pub static T: &[u8] = include_bytes!("table.bin");\n',
        "src/table.bin": b"\x00\x01\x02\xff\xfe" * 100,
        "tests/t.rs": b"fn main() {}\n",
        "benches/b.rs": b"",
        ".github/ci.yml": b"x",
        "src/fixtures/data.txt": b"x",
        "testdata/x": b"x",
    }
    target = tmp_path / "dep-1.0.0"
    build.write_package(target, files, "f" * 64)
    names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert names == {"Cargo.toml", "src/lib.rs", build.CHECKSUM}
    assert "[[test]]" not in (target / "Cargo.toml").read_text()
    assert "0x00u8,0x01,0x02,0xff,0xfe" in (target / "src" / "lib.rs").read_text()
    sums = json.loads((target / build.CHECKSUM).read_text())
    assert sums["package"] == "f" * 64
    assert set(sums["files"]) == {"Cargo.toml", "src/lib.rs"}
    for rel, digest in sums["files"].items():
        assert build.sha256((target / rel).read_bytes()) == digest
    assert build.read_dir(target.parent).binaries == {}


def test_a_file_a_kept_source_includes_stays_even_from_a_dropped_directory(build, tmp_path):
    files = {
        "Cargo.toml": b'[package]\nname = "dep"\n',
        "src/lib.rs": b'pub const T: &str = include_str!("../examples/readme.txt");\n',
        "examples/readme.txt": b"hello\n",
        "examples/other.txt": b"no\n",
    }
    target = tmp_path / "dep-1.0.0"
    build.write_package(target, files, None)
    names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert "examples/readme.txt" in names and "examples/other.txt" not in names


def test_container_files_scripts_and_developer_dotfiles_are_not_vendored(build, tmp_path):
    files = {
        "Cargo.toml": b'[package]\nname = "dep"\n',
        "src/lib.rs": b"",
        "LICENSE": b"licence\n",
        "README.md": b"readme\n",
        "Containerfile": b"FROM scratch\n",
        "Dockerfile.dev": b"FROM scratch\n",
        "tools/Dockerfile": b"FROM scratch\n",
        ".dockerignore": b"target\n",
        "docker-compose.yml": b"x: 1\n",
        "regenerate_bindings.sh": b"#!/bin/sh\n",
        "release.ps1": b"x\n",
        ".travis.yml": b"x: 1\n",
        ".editorconfig": b"root = true\n",
        "justfile": b"x:\n",
        "docs/guide.md": b"x\n",
        "doc/x.md": b"x\n",
        "ci/run.sh": b"x\n",
        "src/helper.sh": b"x\n",
    }
    target = tmp_path / "dep-1.0.0"
    build.write_package(target, files, None)
    names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    # A script below the top is source the crate may run, so only the top-level ones go.
    assert names == {"Cargo.toml", "src/lib.rs", "LICENSE", "README.md", "src/helper.sh", build.CHECKSUM}


def test_a_container_file_a_kept_source_includes_stays(build, tmp_path):
    files = {
        "Cargo.toml": b'[package]\nname = "dep"\n',
        "src/lib.rs": b'pub const T: &str = include_str!("../Containerfile");\n',
        "Containerfile": b"FROM scratch\n",
    }
    target = tmp_path / "dep-1.0.0"
    build.write_package(target, files, None)
    assert (target / "Containerfile").is_file()


def test_include_bytes_of_a_test_only_file_or_the_big_endian_table_is_left_alone(build):
    kept = {"src/lib.rs": b'a(include_bytes!("t.bigendian.bin")); b(include_bytes!("x.bin"));'}
    files = {**kept, "src/t.bigendian.bin": b"\xff" * 4, "src/x.bin": b"\x01\xff"}
    build.inline_bytes(kept, files, {"src/t.bigendian.bin", "src/x.bin"})
    text = kept["src/lib.rs"].decode()
    assert 'include_bytes!("t.bigendian.bin")' in text
    assert "&[0x01u8,0xff]" in text


def test_a_dereferenced_include_bytes_becomes_the_array_itself(build):
    kept = {"src/lib.rs": b'let a = *include_bytes!("x.bin");'}
    build.inline_bytes(kept, {**kept, "src/x.bin": b"\x01\xff"}, {"src/x.bin"})
    assert kept["src/lib.rs"] == b"let a = [0x01u8,0xff];"


def test_a_private_key_in_text_is_defanged_not_refused(build, tmp_path):
    files = {"Cargo.toml": b'[package]\nname = "dep"\n', "src/lib.rs": b"", "src/key.pem": PEM}
    target = tmp_path / "dep-1.0.0"
    build.write_package(target, files, None)
    text = (target / "src" / "key.pem").read_bytes()
    assert b"-----BEGIN PRIVATE KEY-----" not in text and b"----BEGIN PRIVATE KEY-----" in text
    assert build.read_dir(target.parent).binaries == {}


def test_names_windows_cannot_hold_are_refused(build, tmp_path):
    with pytest.raises(build.Problem, match="one file on Windows"):
        build.write_package(tmp_path / "a-1.0.0", {"Cargo.toml": b"", "A.rs": b"", "a.rs": b""}, None)
    with pytest.raises(build.Problem, match="Windows cannot create"):
        build.write_package(tmp_path / "b-1.0.0", {"Cargo.toml": b"", "src/aux.rs": b""}, None)


def test_a_windows_import_library_becomes_hex_that_build_rs_writes_out_and_links(build, tmp_path):
    library = b"!<arch>\n" + bytes(range(256)) * 4
    files = {
        "Cargo.toml": b'[package]\nname = "windows_x86_64_msvc"\n',
        "build.rs": build.WINDOWS_BUILD.encode(),
        "src/lib.rs": b"",
        "lib/windows.0.52.0.lib": library,
    }
    target = tmp_path / "windows_x86_64_msvc-0.52.6"
    build.write_package(target, files, "f" * 64)
    assert not (target / "lib" / "windows.0.52.0.lib").exists()
    digits = "".join((target / "lib" / "windows.0.52.0.lib.hex").read_text().split())
    assert bytes.fromhex(digits) == library
    assert "OUT_DIR" in (target / "build.rs").read_text()
    assert build.read_dir(target.parent).binaries == {}
    changed = dict(files, **{"build.rs": b"fn main() {}\n"})
    with pytest.raises(build.Problem, match="no longer links"):
        build.write_package(tmp_path / "windows_x86_64_msvc-0.52.7", changed, "f" * 64)


@pytest.mark.parametrize("name", ["windows_aarch64_msvc", "windows_i686_gnullvm", "windows_x86_64_gnu", "windows_x86_64_msvc"])
def test_every_windows_arch_crate_is_patched_by_pattern(build, name):
    assert build.package_patch(name) is build.patch_windows_libraries
    assert build.package_patch("windows-sys") is None
    assert build.package_patch("windows") is None


def test_a_crate_with_no_refused_library_is_left_alone(build):
    kept = {"build.rs": build.WINDOWS_BUILD.encode()}
    before = dict(kept)
    build.patch_windows_libraries("windows_x86_64_msvc-0.52.6", kept, {})
    assert kept == before


def test_the_windows_hex_round_trips_through_the_build_script(build, tmp_path):
    rustc = shutil.which("rustc")
    if not rustc:
        pytest.skip("no rustc")
    lib = bytes(range(256)) * 40 + b"!<arch>\n"
    kept, refused = {"build.rs": build.WINDOWS_BUILD.encode()}, {"lib/windows.0.52.0.lib": lib}
    build.patch_windows_libraries("windows_x86_64_msvc-0.52.6", kept, refused)
    assert refused == {}
    lines = kept["lib/windows.0.52.0.lib.hex"].decode().split("\n")
    assert all(len(line) <= 128 for line in lines)
    (tmp_path / "pkg" / "lib").mkdir(parents=True)
    (tmp_path / "pkg" / "lib" / "windows.0.52.0.lib.hex").write_bytes(kept["lib/windows.0.52.0.lib.hex"])
    (tmp_path / "out").mkdir()
    (tmp_path / "build.rs").write_bytes(kept["build.rs"])
    exe = tmp_path / ("build_rs" + (".exe" if os.name == "nt" else ""))
    subprocess.run([rustc, "--edition", "2021", "-o", str(exe), str(tmp_path / "build.rs")], check=True, capture_output=True)
    env = {**os.environ, "CARGO_MANIFEST_DIR": str(tmp_path / "pkg"), "OUT_DIR": str(tmp_path / "out")}
    said = subprocess.run([str(exe)], env=env, check=True, capture_output=True, text=True).stdout
    assert (tmp_path / "out" / "windows.0.52.0.lib").read_bytes() == lib
    assert f"cargo:rustc-link-search=native={tmp_path / 'out'}" in said


def ring_build(build) -> bytes:
    return ("use std::path::{Path, PathBuf};\nuse std::fs;\nfn obj_path(d: &Path, s: &Path) -> PathBuf { d.join(s) }\nfn f(generated_dir: &Path, asm_srcs: Vec<PathBuf>) {\n"
            + build.RING_USE + "\n}\n").encode()


def test_ring_objects_become_hex_decoded_by_build_rs(build, tmp_path):
    obj = b"\x4c\x01" + bytes(range(200))
    files = {
        "Cargo.toml": b'[package]\nname = "ring"\n',
        "build.rs": ring_build(build),
        "pregenerated/x86_64-nasm.o": obj,
        "src/lib.rs": b"",
    }
    target = tmp_path / "ring-0.17.14"
    build.write_package(target, files, "e" * 64)
    assert not (target / "pregenerated" / "x86_64-nasm.o").exists()
    digits = "".join((target / "pregenerated" / "x86_64-nasm.o.hex").read_text().split())
    assert bytes.fromhex(digits) == obj
    text = (target / "build.rs").read_text()
    assert "object_from_text(generated_dir, out_dir, src.as_path())" in text and "fn object_from_text" in text
    with pytest.raises(build.Problem, match="no longer links its objects"):
        build.write_package(tmp_path / "ring-0.17.15", dict(files, **{"build.rs": b"fn main() {}\n"}), "e" * 64)


def test_a_crate_is_unpacked_from_its_archive_and_checked_against_the_lock(build, tmp_path, monkeypatch):
    body = crate_archive("dep-1.2.3", {"Cargo.toml": b"[package]\n", "src/lib.rs": b""})
    crate = build.Crate("dep", "1.2.3", build.sha256(body))
    monkeypatch.setattr(build, "fetch", lambda url: body)
    build.materialize_crate(crate, tmp_path)
    assert (tmp_path / "dep-1.2.3" / "src" / "lib.rs").is_file()
    with pytest.raises(build.Problem, match="refused"):
        build.fetch_crate(build.Crate("dep", "1.2.3", "0" * 64))


def test_a_crate_member_outside_its_directory_is_refused(build):
    with pytest.raises(build.Problem, match="outside"):
        build.archive_files(crate_archive("other-1.0.0", {"x": b""}), "dep-1.2.3/")


def test_the_mirror_url_comes_from_the_environment_then_the_configuration(build, tmp_path, monkeypatch):
    crate = build.Crate("dep", "1.0.0+meta", "0" * 64)
    asked = []
    monkeypatch.setattr(build, "fetch", lambda url: asked.append(url) or b"")
    with pytest.raises(build.Problem):
        build.fetch_crate(crate, "https://mirror.test/{name}/{name}-{version}.crate")
    assert asked == ["https://mirror.test/dep/dep-1.0.0%2Bmeta.crate"]
    cfg = build.parse_config('[deps]\ncrates_url = "https://config.test/{name}-{version}"\n')
    assert build.settings_crates_url(cfg) == "https://config.test/{name}-{version}"
    monkeypatch.setenv("BUILD_CRATES_URL", "https://env.test/{name}-{version}")
    assert build.settings_crates_url(cfg) == "https://env.test/{name}-{version}"
    monkeypatch.delenv("BUILD_CRATES_URL")
    assert build.settings_crates_url(build.parse_config("")) == build.CRATES_URL


def test_workspace_inheritance_is_written_out(build):
    root = {"workspace": {"package": {"version": "1.0.0", "edition": "2021"}, "dependencies": {"x": "2"}}}
    member = {
        "package": {"name": "m", "version": {"workspace": True}, "edition": {"workspace": True}},
        "dependencies": {"x": {"workspace": True, "optional": True}},
    }
    out = build.normalized_manifest(member, root, Path("r/m"), Path("r"), "repo/m")
    assert out["package"]["version"] == "1.0.0"
    assert out["dependencies"]["x"] == {"version": "2", "optional": True}
    bad = {"package": {"name": "m", "version": {"workspace": True}}, "dependencies": {"y": {"workspace": True}}}
    with pytest.raises(build.Problem, match="does not list"):
        build.normalized_manifest(bad, root, Path("r/m"), Path("r"), "repo/m")


def test_toml_is_written_back_with_plain_values_before_subtables(build):
    lines: list[str] = []
    build.toml_table({"package": {"name": "x", "keywords": ["a", "b"]}, "dependencies": {"y": {"version": "1", "features": ["f"]}}}, [], lines)
    parsed = tomllib.loads("\n".join(lines))
    assert parsed["package"]["keywords"] == ["a", "b"] and parsed["dependencies"]["y"]["features"] == ["f"]


# --- git packages, from a repository made here --------------------------------------------------------


@pytest.fixture
def upstream(tmp_path):
    repo = init_repo(tmp_path / "upstream")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "member" / "src").mkdir(parents=True)
    (repo / "member" / "tests").mkdir()
    (repo / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["member"]\n[workspace.package]\nversion = "0.3.0"\nedition = "2021"\n'
        '[workspace.dependencies]\nx = "2"\n',
        encoding="utf-8",
    )
    (repo / "member" / "Cargo.toml").write_text(
        '[package]\nname = "member"\nversion.workspace = true\nedition.workspace = true\n'
        "[dependencies]\nx = { workspace = true, optional = true }\n",
        encoding="utf-8",
    )
    (repo / "member" / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    (repo / "member" / "tests" / "it.rs").write_text("fn t() {}\n", encoding="utf-8")
    return repo, commit_all(repo)


def git_lock(build, tmp_path, repo: Path, commit: str):
    url = repo.resolve().as_uri()
    text = f'[[package]]\nname = "member"\nversion = "0.3.0"\nsource = "git+{url}?rev={commit}#{commit}"\n'
    return write_lock(build, tmp_path, text)


def test_a_git_package_is_vendored_with_its_inheritance_written_out(build, upstream, tmp_path):
    repo, commit = upstream
    lock = git_lock(build, tmp_path, repo, commit)
    deps = tmp_path / "deps"
    changes = build.sync_crates(deps, lock, build.CRATES_URL, 2)
    assert changes == ["added member-0.3.0"]
    package = deps / "crates" / "member-0.3.0"
    manifest = (package / "Cargo.toml").read_text(encoding="utf-8")
    assert manifest.split("\n")[0] == f"# upstream/member at {commit}, with what it inherits written out."
    parsed = tomllib.loads(manifest)
    assert parsed["package"]["version"] == "0.3.0" and parsed["package"]["edition"] == "2021"
    assert parsed["dependencies"]["x"] == {"version": "2", "optional": True}
    assert not (package / "tests").exists()
    assert build.problems(lock, build.read_dir(deps / "crates")) == []
    assert build.sync_crates(deps, lock, build.CRATES_URL, 2) == []


def test_a_git_source_with_a_submodule_is_refused(build, upstream, tmp_path):
    repo, _ = upstream
    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = https://example.test/x\n', encoding="utf-8")
    commit = commit_all(repo, "submodule")
    lock = git_lock(build, tmp_path, repo, commit)
    with pytest.raises(build.Problem, match="submodules"):
        build.sync_crates(tmp_path / "deps", lock, build.CRATES_URL, 1)


def test_a_git_package_the_repository_does_not_have_is_refused(build, upstream, tmp_path):
    repo, commit = upstream
    lock = git_lock(build, tmp_path, repo, commit)
    lock = build.Lock(lock.crates, {r: [("nothere", "9.9.9")] for r in lock.repos}, lock.sources)
    with pytest.raises(build.Problem, match="has 0 packages nothere 9.9.9"):
        build.sync_crates(tmp_path / "deps", lock, build.CRATES_URL, 1)


def test_an_archive_with_a_link_or_a_path_out_of_it_is_refused(build, tmp_path):
    def tar_of(make) -> tarfile.TarFile:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            make(tar)
        return tarfile.open(fileobj=io.BytesIO(out.getvalue()), mode="r")

    def link(tar):
        info = tarfile.TarInfo("a/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    def climb(tar):
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    def absolute(tar):
        info = tarfile.TarInfo("/abs")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    def device(tar):
        info = tarfile.TarInfo("dev")
        info.type = tarfile.CHRTYPE
        tar.addfile(info)

    def good(tar):
        info = tarfile.TarInfo("d/f.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))

    with pytest.raises(build.Problem, match="link"):
        build.safe_extract(tar_of(link), tmp_path / "a")
    build.safe_extract(tar_of(link), tmp_path / "b", links="skip")
    with pytest.raises(build.Problem, match="outside"):
        build.safe_extract(tar_of(climb), tmp_path / "c")
    with pytest.raises(build.Problem, match="outside"):
        build.safe_extract(tar_of(absolute), tmp_path / "d")
    with pytest.raises(build.Problem, match="not a regular file"):
        build.safe_extract(tar_of(device), tmp_path / "e")
    build.safe_extract(tar_of(good), tmp_path / "f")
    assert (tmp_path / "f" / "d" / "f.txt").read_bytes() == b"ok"
    assert not (tmp_path / "escape").exists()


# --- sync and check against a registry made here --------------------------------------------------------


@pytest.fixture
def registry(build, tmp_path, monkeypatch):
    body = crate_archive("dep-1.2.3", {"Cargo.toml": b"[package]\nname = \"dep\"\n", "src/lib.rs": b"", "tests/t.rs": b""})
    checksum = build.sha256(body)
    fetched = []
    monkeypatch.setattr(build, "fetch", lambda url: fetched.append(url) or body)
    root = init_repo(tmp_path / "main")
    (root / "Cargo.lock").write_text(
        LOCK.replace("a" * 64, checksum).split("[[package]]\nname = \"lib\"")[0], encoding="utf-8"
    )
    return root, checksum, fetched


def test_sync_makes_the_checkout_match_and_writes_the_manifest(build, registry, tmp_path, capsys):
    root, checksum, fetched = registry
    ctx = ctx_for(build, root)
    commit_all(root)
    deps = tmp_path / "main-deps"
    deps.mkdir()
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert "added dep-1.2.3" in capsys.readouterr().out
    assert len(fetched) == 1
    manifest = json.loads((deps / "manifest.json").read_text())
    assert manifest["format"] == 1 and manifest["crates"] == 1 and manifest["gitSources"] == 0
    assert manifest["cargoLockSha256"] == build.sha256((root / "Cargo.lock").read_bytes())
    assert manifest["sourceCommit"] == git(root, "rev-parse", "HEAD")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    assert build.main(["--root", str(root), "deps", "check", "--quick", "--deps", str(deps)]) == 0
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert len(fetched) == 1, "a second sync changes nothing and fetches nothing"
    assert ctx.cfg.deps.branch == "deps"


def test_a_stale_package_is_removed_by_sync(build, registry, tmp_path):
    root, _, _ = registry
    ctx_for(build, root)
    deps = tmp_path / "deps"
    vendored(build, deps / "crates", "gone-1.0.0", "c" * 64)
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert not (deps / "crates" / "gone-1.0.0").exists() and (deps / "crates" / "dep-1.2.3").is_dir()


def test_sync_trims_a_package_vendored_before_a_file_was_dropped_and_then_settles(build, registry, tmp_path):
    root, _, fetched = registry
    ctx_for(build, root)
    commit_all(root)
    deps = tmp_path / "main-deps"
    deps.mkdir()
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    folder = deps / "crates" / "dep-1.2.3"
    # What an older tool left: a Containerfile, listed in the checksum like any other file.
    (folder / "Containerfile").write_bytes(b"FROM scratch\n")
    sums = json.loads((folder / build.CHECKSUM).read_text())
    sums["files"]["Containerfile"] = build.sha256(b"FROM scratch\n")
    (folder / build.CHECKSUM).write_text(json.dumps(sums, separators=(",", ":")), encoding="utf-8")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0, "an untrimmed package is not a check failure"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert not (folder / "Containerfile").exists() and len(fetched) == 2
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert len(fetched) == 2, "a trimmed package is current"


def test_sync_leaves_a_package_alone_whose_dropped_looking_file_a_source_includes(build, tmp_path):
    folder = tmp_path / "crates"
    files = {
        "Cargo.toml": b'[package]\nname = "dep"\n',
        "src/lib.rs": b'pub const T: &str = include_str!("../docs/x.md");\n',
        "docs/x.md": b"x\n",
    }
    build.write_package(folder / "dep-1.0.0", files, None)
    tree = build.read_dir(folder)
    assert (folder / "dep-1.0.0" / "docs" / "x.md").is_file()
    assert build.untrimmed(folder, tree, "dep-1.0.0") is False


def test_sync_refuses_a_directory_this_tool_did_not_make(build, registry, tmp_path, capsys):
    root, _, _ = registry
    ctx_for(build, root)
    deps = tmp_path / "deps"
    (deps / "crates" / "mine").mkdir(parents=True)
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 1
    assert "was not made by this tool" in capsys.readouterr().err


def test_check_names_what_is_wrong_and_exits_1(build, registry, tmp_path, capsys):
    root, _, _ = registry
    ctx_for(build, root)
    deps = tmp_path / "deps"
    (deps / "crates").mkdir(parents=True)
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "build.py: crates/dep-1.2.3: missing, Cargo.lock names it" in capsys.readouterr().err


def test_a_manifest_that_disagrees_with_the_lock_is_a_problem_and_an_absent_one_a_note(build, registry, tmp_path, capsys):
    root, _, _ = registry
    ctx_for(build, root)
    deps = tmp_path / "deps"
    build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)])
    capsys.readouterr()
    manifest = json.loads((deps / "manifest.json").read_text())
    manifest["crates"] = 7
    (deps / "manifest.json").write_text(json.dumps(manifest))
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "manifest.json: says 7 crates" in capsys.readouterr().err
    (deps / "manifest.json").unlink()
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    assert "note: no manifest.json" in capsys.readouterr().out


def test_the_manifest_keeps_its_source_commit_when_nothing_else_changed(build, registry, tmp_path):
    root, _, _ = registry
    ctx = ctx_for(build, root)
    first = commit_all(root)
    deps = tmp_path / "deps"
    build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)])
    (root / "README.md").write_text("x")
    commit_all(root, "later")
    build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)])
    assert json.loads((deps / "manifest.json").read_text())["sourceCommit"] == first
    assert ctx.root == root


# --- check --ref: a commit, not a checkout --------------------------------------------------------------


def make_tree_commit(root: Path, entries: dict[str, tuple[str, bytes]]) -> str:
    """A commit holding exactly these paths with modes ("100644" or "120000"), built from trees so
    that a path git's index refuses (a .git component) can be in it."""

    def blob(data: bytes) -> str:
        return subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=data, capture_output=True, check=True).stdout.decode().strip()

    def tree(node: dict) -> str:
        lines = []
        for name, child in sorted(node.items()):
            if isinstance(child, dict):
                lines.append(f"040000 tree {tree(child)}\t{name}")
            else:
                mode, data = child
                lines.append(f"{mode} blob {blob(data)}\t{name}")
        made = subprocess.run(["git", "-C", str(root), "mktree"], input=("\n".join(lines) + "\n").encode(), capture_output=True, check=True)
        return made.stdout.decode().strip()

    top: dict = {}
    for path, value in entries.items():
        node = top
        *dirs, name = path.split("/")
        for part in dirs:
            node = node.setdefault(part, {})
        node[name] = value
    return git(root, "commit-tree", tree(top), "-m", "deps")


def test_check_ref_reports_a_symlink_and_a_dot_git_path_and_a_binary(build, registry, capsys):
    root, checksum, _ = registry
    ctx_for(build, root)
    sums = json.dumps({"files": {"Cargo.toml": build.sha256(b"[package]\n")}, "package": checksum}).encode()
    entries = {
        "crates/dep-1.2.3/Cargo.toml": ("100644", b"[package]\n"),
        f"crates/dep-1.2.3/{build.CHECKSUM}": ("100644", sums),
        "crates/dep-1.2.3/link": ("120000", b"/etc/passwd"),
        "crates/dep-1.2.3/.git/config": ("100644", b"x"),
        "pub/blob.bin": ("100644", FLAGGED["elf executable"]),
        "cdn/www.gstatic.com/flutter-canvaskit/a/canvaskit.wasm": ("100644", FLAGGED["wasm"]),
    }
    commit = make_tree_commit(root, entries)
    assert build.main(["--root", str(root), "deps", "check", "--ref", commit]) == 1
    err = capsys.readouterr().err
    assert "crates/dep-1.2.3/link: a symbolic link" in err
    assert "crates/dep-1.2.3/.git/config: a path below .git" in err
    assert "pub/blob.bin: elf executable" in err
    assert "canvaskit.wasm: wasm" in err
    assert "crates/dep-1.2.3/link: a symbolic link; the branch holds no such file" in err


def test_check_ref_passes_for_a_clean_commit_and_honours_allow(build, registry, capsys):
    root, checksum, _ = registry
    cfg = '[deps]\nallow = ["cdn/www.gstatic.com/flutter-canvaskit/*:wasm"]\n'
    ctx_for(build, root, cfg)
    sums = json.dumps({"files": {"Cargo.toml": build.sha256(b"[package]\n")}, "package": checksum}).encode()
    manifest = json.dumps({"format": 1, "crates": 1, "gitSources": 0}).encode()
    commit = make_tree_commit(
        root,
        {
            "crates/dep-1.2.3/Cargo.toml": ("100644", b"[package]\n"),
            f"crates/dep-1.2.3/{build.CHECKSUM}": ("100644", sums),
            "manifest.json": ("100644", manifest),
            "cdn/www.gstatic.com/flutter-canvaskit/a/canvaskit.wasm": ("100644", FLAGGED["wasm"]),
        },
    )
    assert build.main(["--root", str(root), "deps", "check", "--ref", commit]) == 0
    assert "ok: 1 crates, 0 git sources cover Cargo.lock" in capsys.readouterr().out


def test_check_ref_for_a_commit_that_is_not_here_says_how_to_fetch_it(build, registry, capsys):
    root, _, _ = registry
    ctx_for(build, root)
    assert build.main(["--root", str(root), "deps", "check", "--ref", "f" * 40]) == 1
    assert "is not here: git fetch --depth 1 origin" in capsys.readouterr().err


def test_quick_and_ref_cannot_be_combined(build, registry):
    root, _, _ = registry
    ctx_for(build, root)
    with pytest.raises(SystemExit) as stop:
        build.main(["--root", str(root), "deps", "check", "--quick", "--ref", "HEAD"])
    assert stop.value.code == 2


# --- use ----------------------------------------------------------------------------------------------


def test_the_cargo_config_replaces_every_source_and_stays_offline(build, tmp_path):
    lock = write_lock(build, tmp_path)
    ctx = build.Ctx(tmp_path)
    config = tomllib.loads(build.config_text(ctx, lock, tmp_path / "crates"))
    assert config["net"]["offline"] is True
    assert config["source"]["crates-io"]["replace-with"] == "vendored"
    assert config["source"]["vendored"]["directory"] == (tmp_path / "crates").as_posix()
    git_source = config["source"]["git+https://example.test/org/lib.git?rev=abc123"]
    assert (git_source["git"], git_source["rev"], git_source["replace-with"]) == ("https://example.test/org/lib.git", "abc123", "vendored")


def test_the_source_name_is_a_setting(build, tmp_path):
    ctx = ctx_for(build, tmp_path, '[deps]\nsource_name = "mine"\n')
    assert 'replace-with = "mine"' in build.config_text(ctx, write_lock(build, tmp_path), tmp_path / "c")


def test_use_writes_a_cargo_home_outside_the_repository_only_when_covered(build, tmp_path):
    root = tmp_path / "repo"
    ctx = ctx_for(build, root)
    lock = write_lock(build, root)
    deps = tmp_path / "deps"
    with pytest.raises(build.Problem, match="missing"):
        build.use_crates(ctx, deps, lock, root / "Cargo.lock", tmp_path / "out")
    vendored(build, deps / "crates", "dep-1.2.3", "a" * 64)
    vendored(build, deps / "crates", "lib-0.0.1", None, head=LIB_HEAD)
    info = build.use_crates(ctx, deps, lock, root / "Cargo.lock", tmp_path / "out")
    config = Path(info["config"])
    assert config.parent == (tmp_path / "out" / "cargo-home").resolve()
    assert config.read_text().startswith(build.CONFIG_MARK)
    assert info["crates"] == 1 and info["git_sources"] == 1
    with pytest.raises(build.Problem, match="inside the repository"):
        build.use_crates(ctx, deps, lock, root / "Cargo.lock", root / "target" / "home")
    config.write_text("# mine\n")
    with pytest.raises(build.Problem, match="left alone"):
        build.use_crates(ctx, deps, lock, root / "Cargo.lock", tmp_path / "out")


def test_use_is_quick_by_default_and_full_rehashes(build, tmp_path):
    root = tmp_path / "repo"
    ctx = ctx_for(build, root)
    lock = write_lock(build, root)
    deps = tmp_path / "deps"
    vendored(build, deps / "crates", "dep-1.2.3", "a" * 64)
    vendored(build, deps / "crates", "lib-0.0.1", None, head=LIB_HEAD)
    (deps / "crates" / "dep-1.2.3" / "Cargo.toml").write_bytes(b"[package] # edited\n")
    build.use_crates(ctx, deps, lock, root / "Cargo.lock", tmp_path / "q")
    with pytest.raises(build.Problem, match="SHA-256"):
        build.use_crates(ctx, deps, lock, root / "Cargo.lock", tmp_path / "f", full=True)


def test_use_repo_config_and_json(build, tmp_path, capsys):
    root = tmp_path / "repo"
    ctx_for(build, root)
    write_lock(build, root)
    deps = tmp_path / "deps"
    vendored(build, deps / "crates", "dep-1.2.3", "a" * 64)
    vendored(build, deps / "crates", "lib-0.0.1", None, head=LIB_HEAD)
    argv = ["--root", str(root), "deps", "use", "--deps", str(deps), "--out", str(tmp_path / "out")]
    assert build.main(argv + ["--repo-config", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["repo_config"] == str(root.resolve() / ".cargo" / "config.toml")
    assert (root / ".cargo" / "config.toml").read_text().startswith(build.CONFIG_MARK)
    assert Path(info["cargo_home"]).is_dir()
    (root / ".cargo" / "config.toml").write_text("# mine\n")
    assert build.main(argv + ["--repo-config"]) == 1
    assert "left alone" in capsys.readouterr().err
    (root / ".cargo" / "config.toml").unlink()
    assert build.main(argv) == 0
    out = capsys.readouterr().out
    assert "offline build ready: 1 crates, 1 git repositories" in out and "CARGO_NET_OFFLINE" in out


# --- the deps checkout: path, init, refresh --------------------------------------------------------------------


@pytest.fixture
def main_repo(build, tmp_path, registry):
    root, checksum, fetched = registry
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "build.cfg").write_text(
        '[deps]\npin = { lock = "tools/sdk.lock", item = "core deps" }\n'
        '[[deps.repin]]\nfile = "Containerfile"\npattern = \'^ARG DEPS_REF=(\\S+)$\'\n',
        encoding="utf-8",
    )
    (root / "tools" / "sdk.lock").write_text(f"# lock\ncore rust 1.89.0 any - -\ncore deps {'0' * 40} any - https://example.test/r.git\n", encoding="utf-8")
    commit_all(root, "main")
    return root, checksum, fetched


def test_init_makes_an_orphan_branch_with_readme_and_attributes(build, main_repo, tmp_path, capsys):
    root, _, _ = main_repo
    assert build.main(["--root", str(root), "deps", "init"]) == 0
    deps = tmp_path / "main-deps"
    assert (deps / "README.md").is_file() and (deps / ".gitattributes").read_text() == "* -text\n"
    assert (deps / "crates").is_dir()
    assert git(deps, "rev-parse", "--abbrev-ref", "HEAD") == "deps"
    assert git(deps, "rev-list", "--count", "HEAD") == "2"
    assert subprocess.run(["git", "-C", str(root), "merge-base", "HEAD", "deps"], capture_output=True).returncode == 1, "no common history"
    assert build.main(["--root", str(root), "deps", "init"]) == 1
    assert "exists" in capsys.readouterr().err


def test_path_adds_a_detached_worktree_of_the_remote_branch_and_leaves_a_branch_one_alone(build, main_repo, tmp_path, capsys):
    root, _, _ = main_repo
    build.main(["--root", str(root), "deps", "init"])
    deps = tmp_path / "main-deps"
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()).resolve() == deps.resolve()
    assert git(deps, "rev-parse", "--abbrev-ref", "HEAD") == "deps", "a worktree on a branch stays there"
    origin = init_repo(tmp_path / "origin.git")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "deps")
    git(root, "fetch", "-q", "origin")
    git(root, "worktree", "remove", "--force", str(deps))
    git(root, "branch", "-D", "deps")
    assert build.main(["--root", str(root), "deps", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()).resolve() == deps.resolve()
    assert git(deps, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "added detached"
    assert git(deps, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/deps")
    assert build.main(["--root", str(root), "deps", "refresh"]) == 1
    assert "is not on a branch" in capsys.readouterr().err


def test_path_without_a_branch_says_how_to_get_one(build, main_repo, capsys):
    root, _, _ = main_repo
    assert build.main(["--root", str(root), "deps", "path"]) == 1
    assert "no deps checkout" in capsys.readouterr().err


def test_the_environment_and_the_setting_name_the_checkout(build, main_repo, tmp_path, monkeypatch, capsys):
    root, _, _ = main_repo
    monkeypatch.setenv("BUILD_DEPS", str(tmp_path / "from-env"))
    assert build.main(["--root", str(root), "deps", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == (tmp_path / "from-env").resolve()
    monkeypatch.delenv("BUILD_DEPS")
    (root / "tools" / "build.cfg").write_text('[deps]\ncheckout = "elsewhere"\n', encoding="utf-8")
    assert build.main(["--root", str(root), "deps", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == (root / "elsewhere").resolve()


def test_refresh_commits_on_deps_repins_files_then_pins_main_and_a_second_run_is_current(build, main_repo, tmp_path, capsys):
    root, _, fetched = main_repo
    build.main(["--root", str(root), "deps", "init"])
    deps = tmp_path / "main-deps"
    (deps / "Containerfile").write_bytes(("FROM scratch\nARG DEPS_REF=" + "1" * 40 + "\n").encode())
    git(deps, "add", "Containerfile")
    git(deps, "commit", "-q", "-m", "chore: Containerfile")
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "refresh"]) == 0
    out = capsys.readouterr().out
    assert "added dep-1.2.3" in out and "pinned deps" in out
    source = git(root, "rev-parse", "--short=8", "HEAD~1")
    subjects = git(deps, "log", "--format=%s", "-3").split("\n")
    assert subjects[0] == f"chore: Containerfile pin deps at {git(deps, 'rev-parse', '--short=8', 'HEAD~1')}"
    assert subjects[1] == f"chore: crates/ for main at {source}"
    crates_commit = git(deps, "rev-parse", "HEAD~1")
    assert f"ARG DEPS_REF={crates_commit}" in (deps / "Containerfile").read_text()
    assert git(root, "log", "-1", "--format=%s") == f"chore: sdk.lock pins deps at {crates_commit[:8]}"
    assert f"core deps {crates_commit} any" in (root / "tools" / "sdk.lock").read_text()
    assert build.main(["--root", str(root), "deps", "check", "--ref", crates_commit]) == 0
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "refresh"]) == 0
    assert f"current: deps {crates_commit} covers Cargo.lock" in capsys.readouterr().out
    assert len(fetched) == 1
    assert git(root, "status", "--porcelain") == ""


def test_refresh_refuses_changes_outside_what_it_generates(build, main_repo, tmp_path, capsys):
    root, _, _ = main_repo
    build.main(["--root", str(root), "deps", "init"])
    (tmp_path / "main-deps" / "notes.txt").write_text("x")
    assert build.main(["--root", str(root), "deps", "refresh"]) == 1
    assert "has changes outside" in capsys.readouterr().err


def test_a_repin_pattern_that_matches_nothing_is_a_problem(build, main_repo, tmp_path, capsys):
    root, _, _ = main_repo
    build.main(["--root", str(root), "deps", "init"])
    (tmp_path / "main-deps" / "Containerfile").write_text("FROM scratch\n")
    git(tmp_path / "main-deps", "add", "Containerfile")
    git(tmp_path / "main-deps", "commit", "-q", "-m", "c")
    assert build.main(["--root", str(root), "deps", "refresh"]) == 1
    assert "no match for" in capsys.readouterr().err


def test_repin_replaces_only_the_group(build):
    import re

    found = re.search(r"^(ARG X=)(\S+)(;)$", "ARG X=old;", re.M)
    assert build.repin_match(re.search(r"^ARG X=(\S+);$", "ARG X=old;", re.M), "new") == "ARG X=new;"
    assert found and build.repin_match(re.search(r"old", "an old one"), "new") == "new"


# --- scan -----------------------------------------------------------------------------------------------------


@pytest.fixture
def scan_repo(build, tmp_path):
    root = init_repo(tmp_path / "scan")
    ctx_for(build, root)
    for name, body in {**{f"flag/{k}.dat": v for k, v in FLAGGED.items()}, **{f"ok/{k}.dat": v for k, v in PASSING.items()}}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
    return root


def test_scan_names_every_refused_kind_and_fails(build, scan_repo, capsys):
    commit_all(scan_repo)
    assert build.main(["--root", str(scan_repo), "scan"]) == 1
    out = capsys.readouterr().out
    for kind in FLAGGED:
        assert f"FLAG {kind}: 1" in out, kind
    last = out.strip().split("\n")[-1]
    assert last == f"FAIL: {len(FLAGGED)} flagged, {len(FLAGGED) + len(PASSING) + 1} paths scanned"
    for label in PASSING:
        assert f"ok/{label}.dat" not in out


def test_scan_passes_clean_text_and_images_with_the_contract_line(build, tmp_path, capsys):
    root = init_repo(tmp_path / "clean")
    ctx_for(build, root)
    for label, body in PASSING.items():
        (root / f"{label.replace(' ', '_')}.dat").write_bytes(body)
    commit_all(root)
    assert build.main(["--root", str(root), "scan", "HEAD"]) == 0
    assert capsys.readouterr().out.strip().split("\n")[-1] == f"PASS: 0 flagged, {len(PASSING) + 1} paths scanned"


def test_scan_all_refs_finds_a_blob_that_history_still_holds(build, tmp_path, capsys):
    root = init_repo(tmp_path / "history")
    ctx_for(build, root)
    (root / "a.txt").write_text("fine\n")
    (root / "secret.bin").write_bytes(FLAGGED["elf executable"])
    commit_all(root)
    (root / "secret.bin").unlink()
    commit_all(root, "remove")
    assert build.main(["--root", str(root), "scan", "HEAD"]) == 0
    capsys.readouterr()
    assert build.main(["--root", str(root), "scan", "--all-refs"]) == 1
    out = capsys.readouterr().out
    assert "FLAG elf executable: 1" in out and "secret.bin" in out


def test_scan_passes_legacy_text_in_history_and_prints_a_note(build, tmp_path, capsys):
    root = init_repo(tmp_path / "legacy")
    ctx_for(build, root)
    (root / "README.md").write_bytes(MIXED_MARKDOWN)
    commit_all(root)
    (root / "README.md").write_bytes(MIXED_MARKDOWN.replace(b"\xed", b"i"))
    commit_all(root, "fix the byte")
    for argv in (["--all-refs"], ["HEAD~1"], ["--tree", str(root)]):
        capsys.readouterr()
        assert build.main(["--root", str(root), "scan", *argv]) == 0, argv
        out = capsys.readouterr().out
        if argv[0] != "--tree":
            assert "NOTE text, not UTF-8: 1" in out and "README.md" in out and "FLAG" not in out, argv
        assert out.strip().split("\n")[-1].startswith("PASS: 0 flagged")


def test_scan_a_tree_and_allow(build, tmp_path, capsys):
    root = tmp_path / "plain"
    ctx_for(build, root, '[deps]\nallow = ["cdn/*:wasm"]\n')
    (root / "cdn").mkdir()
    (root / "cdn" / "x.wasm").write_bytes(FLAGGED["wasm"])
    (root / "other.wasm").write_bytes(FLAGGED["wasm"])
    assert build.main(["--root", str(root), "scan", "--tree", str(root)]) == 1
    out = capsys.readouterr().out
    assert "other.wasm" in out and "cdn/x.wasm" not in out
    assert "paths scanned" in out


def test_scan_reports_a_symlink_and_a_dot_git_path_in_a_commit(build, tmp_path, capsys):
    root = init_repo(tmp_path / "links")
    ctx_for(build, root)
    commit = make_tree_commit(root, {"a/link": ("120000", b"target"), "b/.git/config": ("100644", b"x"), "c.txt": ("100644", b"ok")})
    assert build.main(["--root", str(root), "scan", commit]) == 1
    out = capsys.readouterr().out
    assert "FLAG a symbolic link: 1" in out and "FLAG a path below .git: 1" in out


def test_scan_lists_at_most_25_and_counts_the_rest(build, tmp_path, capsys):
    root = init_repo(tmp_path / "many")
    ctx_for(build, root)
    for i in range(27):
        (root / f"f{i:02}.bin").write_bytes(FLAGGED["elf executable"] + bytes([i]))
    commit_all(root)
    assert build.main(["--root", str(root), "scan"]) == 1
    out = capsys.readouterr().out
    assert "FLAG elf executable: 27" in out and "... 2 more" in out


def test_scan_takes_one_mode_at_a_time(build, tmp_path):
    root = init_repo(tmp_path / "modes")
    ctx_for(build, root)
    with pytest.raises(SystemExit) as stop:
        build.main(["--root", str(root), "scan", "HEAD", "--all-refs"])
    assert stop.value.code == 2


def test_refresh_without_a_pin_says_current_when_nothing_changed(build, registry, tmp_path, capsys):
    root, _, fetched = registry
    ctx_for(build, root)
    commit_all(root)
    build.main(["--root", str(root), "deps", "init"])
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "refresh"]) == 0
    assert "pinned deps" in capsys.readouterr().out
    assert build.main(["--root", str(root), "deps", "refresh"]) == 0
    assert "current: deps " in capsys.readouterr().out
    assert len(fetched) == 1


# --- several vendor folders, manifest format 2, an item vendor -----------------------------------------------------


def vendor_tables(*tables: tuple[str, str, str]) -> str:
    """`[[deps.vendor]]` tables from (folder, key, value)."""
    return "".join(f'[[deps.vendor]]\nfolder = "{folder}"\n{key} = "{value}"\n' for folder, key, value in tables)


def second_revision(repo: Path) -> str:
    (repo / "member" / "src" / "lib.rs").write_text("pub fn g() {}\n", encoding="utf-8")
    return commit_all(repo, "second")


def test_two_vendor_folders_hold_two_revisions_of_one_package(build, upstream, tmp_path, capsys):
    repo, first = upstream
    second = second_revision(repo)
    root = init_repo(tmp_path / "main")
    git_lock(build, root, repo, first)
    (root / "app").mkdir()
    git_lock(build, root / "app", repo, second)
    ctx_for(build, root, vendor_tables(("crates", "lock", "Cargo.lock"), ("crates-app", "lock", "app/Cargo.lock")))
    deps = tmp_path / "deps"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    heads = [(deps / f / "member-0.3.0" / "Cargo.toml").read_text().split("\n")[0] for f in ("crates", "crates-app")]
    assert f"at {first}," in heads[0] and f"at {second}," in heads[1]
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    out = capsys.readouterr().out
    assert "ok: crates 0 crates, 1 git sources cover Cargo.lock" in out and "ok: crates-app 0 crates, 1 git sources cover app/Cargo.lock" in out
    manifest = json.loads((deps / "manifest.json").read_text())
    assert manifest["format"] == 2 and "crates" not in manifest and "cargoLockSha256" not in manifest
    assert set(manifest["vendors"]) == {"crates", "crates-app"}
    assert manifest["vendors"]["crates-app"] == {
        "lock": "app/Cargo.lock", "lockSha256": build.sha256((root / "app" / "Cargo.lock").read_bytes()), "crates": 0, "gitSources": 1,
    }
    shutil.rmtree(deps / "crates-app" / "member-0.3.0")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "crates-app/member-0.3.0: missing, Cargo.lock names it" in capsys.readouterr().err
    assert build.main(["--root", str(root), "deps", "check", "--vendor", "crates", "--deps", str(deps)]) == 0


def test_vendor_picks_one_folder_for_sync_check_use_and_offline_plan(build, registry, tmp_path, capsys):
    root, _, fetched = registry
    (root / "other").mkdir()
    shutil.copyfile(root / "Cargo.lock", root / "other" / "Cargo.lock")
    ctx_for(build, root, vendor_tables(("crates", "lock", "Cargo.lock"), ("crates-other", "lock", "other/Cargo.lock")))
    deps = tmp_path / "deps"
    base = ["--root", str(root), "deps"]
    assert build.main([*base, "sync", "--vendor", "crates-other", "--deps", str(deps)]) == 0
    assert (deps / "crates-other" / "dep-1.2.3").is_dir() and not (deps / "crates").exists()
    assert set(json.loads((deps / "manifest.json").read_text())["vendors"]) == {"crates-other"}
    assert build.main([*base, "check", "--vendor", "crates-other", "--deps", str(deps)]) == 0
    assert build.main([*base, "check", "--deps", str(deps)]) == 1, "the primary folder is not there"
    assert build.main([*base, "sync", "--deps", str(deps)]) == 0
    assert set(json.loads((deps / "manifest.json").read_text())["vendors"]) == {"crates", "crates-other"}
    capsys.readouterr()
    assert build.main([*base, "use", "--vendor", "crates-other", "--json", "--out", str(tmp_path / "o"), "--deps", str(deps)]) == 0
    config = Path(json.loads(capsys.readouterr().out)["config"]).read_text()
    assert (deps / "crates-other").resolve().as_posix() in config
    assert build.main([*base, "use", "--json", "--out", str(tmp_path / "p"), "--deps", str(deps)]) == 0
    assert (deps / "crates").resolve().as_posix() in Path(json.loads(capsys.readouterr().out)["config"]).read_text()
    assert build.main(["--root", str(root), "offline", "plan", "--json", "--vendor", "crates-other", "--deps", str(deps), "--out", str(tmp_path / "q")]) == 0
    assert json.loads(capsys.readouterr().out)["cargo_home"]
    with pytest.raises(SystemExit) as stop:
        build.main([*base, "check", "--vendor", "nope", "--deps", str(deps)])
    assert stop.value.code == 2 and "the folders are crates, crates-other" in capsys.readouterr().err


def test_a_vendor_names_exactly_one_of_lock_and_item_and_a_folder_once(build, tmp_path):
    for text, message in (
        ('[[deps.vendor]]\nfolder = "a"\n', "exactly one of lock and item"),
        ('[[deps.vendor]]\nfolder = "a"\nlock = "x"\nitem = "app y"\n', "exactly one of lock and item"),
        (vendor_tables(("a", "lock", "x"), ("a", "lock", "y")), "listed twice"),
        (vendor_tables(("a/b", "lock", "x")), "not one path segment"),
    ):
        with pytest.raises(build.Problem, match=message):
            build.vendors_of(ctx_for(build, tmp_path / "r", text))


def tool_registry(build, monkeypatch):
    """A crates.io that serves `tool` (whose own Cargo.lock names `dep`) and `dep`."""
    dep = crate_archive("dep-1.2.3", {"Cargo.toml": b'[package]\nname = "dep"\n', "src/lib.rs": b""})
    lock = (
        'version = 4\n[[package]]\nname = "tool"\nversion = "1.0.0"\n[[package]]\nname = "dep"\nversion = "1.2.3"\n'
        f'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "{build.sha256(dep)}"\n'
    )
    tool = crate_archive("tool-1.0.0", {"Cargo.toml": b'[package]\nname = "tool"\n', "src/main.rs": b"", "Cargo.lock": lock.encode()})
    served = {"dep-1.2.3.crate": dep, "tool-1.0.0.crate": tool}
    monkeypatch.setattr(build, "fetch", lambda url: served[url.rsplit("/", 1)[1]])
    return build.sha256(tool)


def item_root(build, tmp_path, checksum: str):
    root = init_repo(tmp_path / "main")
    ctx_for(build, root, '[[deps.vendor]]\nfolder = "crates-tool"\nitem = "app tool"\nbin = "tool-bin"\n')
    (root / "tools" / "sdk.lock").write_text(f"app tool 1.0.0 any {checksum} -\n", encoding="utf-8")
    return root


def test_an_item_vendor_is_the_crate_and_the_packages_of_its_own_lock(build, tmp_path, monkeypatch, capsys):
    checksum = tool_registry(build, monkeypatch)
    root = item_root(build, tmp_path, checksum)
    deps = tmp_path / "deps"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert sorted(p.name for p in (deps / "crates-tool").iterdir()) == ["dep-1.2.3", "tool-1.0.0"]
    assert (deps / "crates-tool" / "tool-1.0.0" / "Cargo.lock").is_file()
    entry = json.loads((deps / "manifest.json").read_text())["vendors"]["crates-tool"]
    assert entry["item"] == "app tool" and entry["crates"] == 2 and entry["gitSources"] == 0
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    assert "ok: crates-tool 2 crates, 0 git sources cover the app tool crate's lock" in capsys.readouterr().out
    (deps / "crates-tool" / "dep-1.2.3" / "Cargo.toml").write_bytes(b"[package] # edited\n")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "crates-tool/dep-1.2.3/Cargo.toml: SHA-256" in capsys.readouterr().err
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    with pytest.raises(SystemExit):
        build.main(["--root", str(root), "deps", "check", "--cargo-lock", str(root / "x"), "--deps", str(deps)])
    assert "cannot replace crates-tool" in capsys.readouterr().err


def test_sync_refuses_an_item_row_without_a_checksum_and_check_only_notes_it(build, tmp_path, monkeypatch, capsys):
    checksum = tool_registry(build, monkeypatch)
    root = item_root(build, tmp_path, "-")
    deps = tmp_path / "deps"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 1
    err = capsys.readouterr().err
    assert "the `app tool` row has no SHA-256" in err and "https://index.crates.io/" in err
    (root / "tools" / "sdk.lock").write_text(f"app tool 1.0.0 any {checksum} -\n", encoding="utf-8")
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    (root / "tools" / "sdk.lock").write_text("app tool 1.0.0 any - -\n", encoding="utf-8")
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    assert "the `app tool` row has no SHA-256, so crates-tool is not held to the crate's checksum" in capsys.readouterr().out


def test_format_1_is_written_as_it_always_was_when_nothing_is_configured(build, registry, tmp_path):
    root, checksum, _ = registry
    ctx_for(build, root)
    commit = commit_all(root)
    deps = tmp_path / "deps"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    expected = (
        '{\n  "cargoLockSha256": "%s",\n  "crates": 1,\n  "format": 1,\n  "gitSources": 0,\n  "sourceCommit": "%s"\n}\n'
        % (build.sha256((root / "Cargo.lock").read_bytes()), commit)
    )
    assert (deps / "manifest.json").read_bytes() == expected.encode()


def test_the_manifest_keeps_another_writers_keys_and_drops_a_section_turned_off(build, registry, tmp_path):
    root, _, _ = registry
    commit_all(root)
    for cfg, format_ in (("", 1), (vendor_tables(("crates", "lock", "Cargo.lock")), 2)):
        ctx_for(build, root, cfg)
        deps = tmp_path / f"deps{format_}"
        deps.mkdir()
        (deps / "manifest.json").write_text(json.dumps({"format": 1, "officeJs": {"commit": "x"}, "cdn": {"files": []}, "trees": {}}), encoding="utf-8")
        assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
        manifest = json.loads((deps / "manifest.json").read_text())
        assert manifest["format"] == format_ and manifest["officeJs"] == {"commit": "x"}
        assert "cdn" not in manifest and "trees" not in manifest, "an owned section that is off disappears"


def test_a_format_2_manifest_without_vendors_gives_notes_and_an_unknown_format_is_a_problem(build, registry, tmp_path, capsys):
    root, _, _ = registry
    ctx_for(build, root, vendor_tables(("crates", "lock", "Cargo.lock")))
    deps = tmp_path / "deps"
    assert build.main(["--root", str(root), "deps", "sync", "--deps", str(deps)]) == 0
    (deps / "manifest.json").write_text(json.dumps({"format": 2, "officeJs": {}}), encoding="utf-8")
    capsys.readouterr()
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 0
    assert "note: manifest.json does not record crates" in capsys.readouterr().out
    (deps / "manifest.json").write_text(json.dumps({"format": 2, "vendors": {"crates": {"crates": 9, "gitSources": 0}}}), encoding="utf-8")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "manifest.json: crates: says 9 crates" in capsys.readouterr().err
    (deps / "manifest.json").write_text(json.dumps({"format": 3}), encoding="utf-8")
    assert build.main(["--root", str(root), "deps", "check", "--deps", str(deps)]) == 1
    assert "format 3, this tool reads 1 and 2" in capsys.readouterr().err


# --- a whole repository tree, unmodified, at a pinned commit --------------------------------------------------------

TREE_COMMIT = "c" * 40
TREE_FILES = {
    "LICENSE.md": b"Original form only.\r\nSecond line.\r\n",
    "dist/a.js": b"var a = 1;\r\n",
    "dist/sub/b.js": b"var b = 2;\n",
}
TREE_CFG = (
    '[[deps.tree]]\nname = "lib"\nrepo = "owner/lib"\npin = "tools/lib.json"\npath = "cdn/lib"\nlicense = "LICENSE.md"\n'
)


def tree_pin_text(build, files: dict[str, bytes], **more) -> str:
    pin = {"repository": "owner/lib", "commit": TREE_COMMIT, "carried": ["LICENSE.md"], "files": {p: build.sha256(b) for p, b in sorted(files.items())}}
    return json.dumps({**pin, **more}, indent=1) + "\n"


@pytest.fixture
def tree_repo(build, tmp_path, monkeypatch):
    root = init_repo(tmp_path / "main")
    (root / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    ctx_for(build, root, TREE_CFG)
    (root / "tools" / "lib.json").write_text(tree_pin_text(build, TREE_FILES), encoding="utf-8")
    asked: list[str] = []
    served = {"body": crate_archive("lib-cccc", TREE_FILES)}

    def fake(url):
        asked.append(url)
        return served["body"]

    monkeypatch.setattr(build, "fetch", fake)
    commit_all(root)
    return root, tmp_path / "deps", asked, served


def run_deps(build, root, deps, *words) -> int:
    return build.main(["--root", str(root), "deps", *words, "--deps", str(deps)])


def test_a_tree_is_written_unmodified_recorded_and_left_alone_by_a_second_sync(build, tree_repo, capsys):
    root, deps, asked, _ = tree_repo
    assert run_deps(build, root, deps, "sync") == 0
    assert asked == ["https://codeload.github.com/owner/lib/tar.gz/" + TREE_COMMIT]
    for rel, body in TREE_FILES.items():
        assert (deps / "cdn" / "lib" / rel).read_bytes() == body, "no trimming and no line-ending change"
    entry = json.loads((deps / "manifest.json").read_text())["trees"]["lib"]
    assert entry["commit"] == TREE_COMMIT and entry["path"] == "cdn/lib" and entry["repository"] == "owner/lib"
    assert {f["path"] for f in entry["files"]} == set(TREE_FILES) and entry["files"][0]["bytes"] == len(TREE_FILES["LICENSE.md"])
    assert run_deps(build, root, deps, "sync") == 0 and len(asked) == 1, "files that match the pin are left, and nothing is fetched"
    capsys.readouterr()
    assert run_deps(build, root, deps, "check") == 0
    assert "cdn/lib" not in capsys.readouterr().out
    (deps / "cdn" / "lib" / "extra.txt").write_text("x", encoding="utf-8")
    (deps / "cdn" / "lib" / "dist" / "a.js").write_bytes(b"edited")
    assert run_deps(build, root, deps, "sync") == 0
    assert not (deps / "cdn" / "lib" / "extra.txt").exists() and (deps / "cdn" / "lib" / "dist" / "a.js").read_bytes() == TREE_FILES["dist/a.js"]
    assert len(asked) == 2


def test_an_archive_that_is_not_the_pinned_commit_is_refused_and_writes_nothing(build, tree_repo, capsys):
    root, deps, _, served = tree_repo
    served["body"] = crate_archive("lib-cccc", {**TREE_FILES, "dist/a.js": b"var a = 2;\n"})
    assert run_deps(build, root, deps, "sync") == 1
    assert "is not what tools/lib.json pins: 1 files differ, the first is dist/a.js" in capsys.readouterr().err
    served["body"] = crate_archive("lib-cccc", {**TREE_FILES, "more.js": b"x"})
    assert run_deps(build, root, deps, "sync") == 1
    assert "it holds more.js, which the pin does not name" in capsys.readouterr().err
    served["body"] = crate_archive("lib-cccc", {"LICENSE.md": TREE_FILES["LICENSE.md"]})
    assert run_deps(build, root, deps, "sync") == 1
    assert "which it lacks" in capsys.readouterr().err
    assert not (deps / "cdn").exists()


def test_a_link_or_a_path_out_of_the_archive_is_refused(build, tree_repo, capsys):
    root, deps, _, served = tree_repo

    def archive(make) -> bytes:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            make(tar)
        return out.getvalue()

    def link(tar):
        info = tarfile.TarInfo("lib-cccc/dist/a.js")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    def climb(tar):
        info = tarfile.TarInfo("lib-cccc/../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    served["body"] = archive(link)
    assert run_deps(build, root, deps, "sync") == 1
    assert "not a regular file" in capsys.readouterr().err
    served["body"] = archive(climb)
    assert run_deps(build, root, deps, "sync") == 1
    assert "outside its directory" in capsys.readouterr().err
    assert not (deps.parent / "escape").exists()


def test_a_file_the_scanner_flags_is_refused_until_allow_names_it(build, tree_repo, capsys):
    root, deps, _, served = tree_repo
    files = {**TREE_FILES, "dist/tool.bin": FLAGGED["elf executable"]}
    (root / "tools" / "lib.json").write_text(tree_pin_text(build, files), encoding="utf-8")
    served["body"] = crate_archive("lib-cccc", files)
    assert run_deps(build, root, deps, "sync") == 1
    err = capsys.readouterr().err
    assert "cdn/lib/dist/tool.bin: elf executable" in err and "[deps] allow" in err
    assert not (deps / "cdn").exists(), "nothing is dropped silently, and nothing is written"
    ctx_for(build, root, TREE_CFG + '[deps]\nallow = ["cdn/lib/dist/*.bin:elf executable"]\n')
    assert run_deps(build, root, deps, "sync") == 0
    assert (deps / "cdn" / "lib" / "dist" / "tool.bin").read_bytes() == FLAGGED["elf executable"]
    ctx_for(build, root, TREE_CFG)
    assert run_deps(build, root, deps, "sync") == 1, "a file already there is held to the rule too"


def test_check_names_a_missing_a_changed_and_an_extra_file_and_quick_skips_the_hash(build, tree_repo, capsys):
    root, deps, _, _ = tree_repo
    run_deps(build, root, deps, "sync")
    capsys.readouterr()
    (deps / "cdn" / "lib" / "dist" / "sub" / "b.js").unlink()
    (deps / "cdn" / "lib" / "dist" / "a.js").write_bytes(b"edited")
    (deps / "cdn" / "lib" / "more.txt").write_text("x", encoding="utf-8")
    assert run_deps(build, root, deps, "check") == 1
    err = capsys.readouterr().err
    assert "cdn/lib/dist/sub/b.js: missing, tools/lib.json names it" in err
    assert "cdn/lib/dist/a.js: SHA-256 is not the pin's" in err and "cdn/lib/more.txt: not in tools/lib.json" in err
    assert run_deps(build, root, deps, "check", "--quick") == 1
    err = capsys.readouterr().err
    assert "SHA-256" not in err and "missing" in err


def test_a_manifest_without_trees_is_a_note_and_one_with_another_commit_a_problem(build, tree_repo, capsys):
    root, deps, _, _ = tree_repo
    run_deps(build, root, deps, "sync")
    manifest = json.loads((deps / "manifest.json").read_text())
    entry = manifest.pop("trees")
    (deps / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    capsys.readouterr()
    assert run_deps(build, root, deps, "check") == 0
    assert "note: manifest.json records no trees.lib" in capsys.readouterr().out
    manifest["trees"] = {"lib": {**entry["lib"], "commit": "d" * 40}}
    (deps / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert run_deps(build, root, deps, "check") == 1
    assert f"trees.lib is at {'d' * 40}, tools/lib.json pins {TREE_COMMIT}" in capsys.readouterr().err


def test_tree_pin_rewrites_the_commit_and_files_and_keeps_the_repositorys_keys(build, tree_repo, capsys):
    root, deps, asked, served = tree_repo
    new = {"LICENSE.md": b"New\n", "dist/c.js": b"c\n"}
    served["body"] = crate_archive("lib-dddd", new)
    before = json.loads((root / "tools" / "lib.json").read_text())
    assert build.main(["--root", str(root), "deps", "tree-pin", "lib", "d" * 40]) == 0
    assert asked[-1].endswith("/tar.gz/" + "d" * 40)
    text = (root / "tools" / "lib.json").read_bytes()
    after = json.loads(text)
    assert list(after) == list(before), "the key order is kept"
    assert after["commit"] == "d" * 40 and after["carried"] == ["LICENSE.md"] and after["repository"] == "owner/lib"
    assert after["files"] == {p: build.sha256(b) for p, b in sorted(new.items())}
    assert text == (json.dumps(after, indent=1) + "\n").encode() and b"\r" not in text
    with pytest.raises(SystemExit):
        build.main(["--root", str(root), "deps", "tree-pin", "lib", "main"])
    assert "a branch name would let the pin float" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        build.main(["--root", str(root), "deps", "tree-pin", "nope", "d" * 40])
    assert "no [[deps.tree]] named nope" in capsys.readouterr().err


def test_the_cdn_step_and_the_tree_step_leave_each_others_files_in_either_order(build, tree_repo, monkeypatch):
    root, deps, _, served = tree_repo
    css = b"body {}\n"
    (root / "tools" / "cdn.json").write_text(
        json.dumps({"files": [{"host": "cdn.example.test", "path": "/x/site.css", "sha256": build.sha256(css)}]}), encoding="utf-8"
    )
    ctx_for(build, root, TREE_CFG + '[deps.cdn]\nindex = "tools/cdn.json"\n')
    monkeypatch.setattr(build, "fetch", lambda url: served["body"] if "codeload" in url else css)
    ctx = build.Ctx(root)
    for order in ((build.sync_cdn, build.sync_trees), (build.sync_trees, build.sync_cdn), (build.sync_cdn, build.sync_trees)):
        for step in order:
            step(ctx, deps, None)
        assert (deps / "cdn" / "cdn.example.test" / "x" / "site.css").read_bytes() == css
        assert (deps / "cdn" / "lib" / "dist" / "a.js").read_bytes() == TREE_FILES["dist/a.js"]
    (deps / "cdn" / "other.txt").write_text("x", encoding="utf-8")
    changes, _ = build.sync_cdn(ctx, deps, None)
    assert changes == ["removed cdn/other.txt"], "the CDN step still removes what is not its own or a tree's"
