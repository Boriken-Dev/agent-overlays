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
