"""`build.py release`: the matrix, building and packing per target, the universal binary, images, verify."""

from __future__ import annotations

import gzip
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from conftest import init_repo
from test_build_deps import ctx_for, vendored, write_lock, LIB_HEAD

CFG = """
[release]
name = "tool"

[[release.packages]]
package = "tool-cli"
bin = "tool"
features = ["cli", "tls"]

[[release.packages]]
package = "helper"

[[release.target]]
triple = "x86_64-unknown-linux-musl"
runner = "ubuntu-latest"
env = { "CC_{triple_}" = "musl-gcc", "CARGO_TARGET_{triple_}_LINKER" = "musl-gcc" }

[[release.target]]
triple = "aarch64-unknown-linux-musl"
runner = "ubuntu-24.04-arm"
env = { "CC_{triple_}" = "musl-gcc" }

[[release.target]]
triple = "x86_64-pc-windows-msvc"
runner = "windows-latest"
rustflags = ["-C", "target-feature=+crt-static"]
archive = "zip"

[[release.target]]
triple = "aarch64-apple-darwin"
runner = "macos-latest"

[[release.target]]
triple = "x86_64-apple-darwin"
runner = "macos-latest"

[release.universal]
macos = ["aarch64-apple-darwin", "x86_64-apple-darwin"]

[release.image]
file = "packaging/Containerfile"
archs = { amd64 = "ubuntu-latest", arm64 = "ubuntu-24.04-arm" }
"""


@pytest.fixture
def repo(build, tmp_path):
    root = init_repo(tmp_path / "repo")
    ctx_for(build, root, CFG)
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["a"]\n[workspace.package]\nversion = "1.2.3"\n', encoding="utf-8")
    (root / "LICENSE").write_text("licence\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "notes.txt").write_text("not shipped\n", encoding="utf-8")
    return root


class Stub:
    """Stands in for cargo, lipo and the container tools: records commands and makes their outputs."""

    def __init__(self, build, root, monkeypatch):
        self.root = root
        self.commands: list[list[str]] = []
        self.captured: list[list[str]] = []
        self.envs: list[dict] = []
        self.info = "Architectures in the fat file: out are: x86_64 arm64"
        monkeypatch.setattr(build, "run_tool", self.run)
        monkeypatch.setattr(build, "capture_tool", self.capture)

    def run(self, command, env=None, cwd=None):
        command = [str(c) for c in command]
        self.commands.append(command)
        self.envs.append(env or {})
        if "build" in command and "--target" in command:
            triple = command[command.index("--target") + 1]
            package = command[command.index("-p") + 1]
            binary = command[command.index("--bin") + 1] if "--bin" in command else package
            if "windows" in triple:
                binary += ".exe"
            path = self.root / "target" / triple / "release" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"binary {triple} {binary}".encode())
        elif command[0] == "lipo" and command[1] == "-create":
            Path(command[3]).write_bytes(b"fat " + b"+".join(Path(p).read_bytes() for p in command[4:]))
        elif command[1:2] == ["save"]:
            Path(command[command.index("-o") + 1]).write_bytes(b"image archive")

    def capture(self, command, env=None):
        command = [str(c) for c in command]
        self.captured.append(command)
        if command[0] == "lipo":
            return 0, self.info
        return 0, "ELF 64-bit LSB executable, statically linked, stripped"


@pytest.fixture
def stub(build, repo, monkeypatch):
    return Stub(build, repo, monkeypatch)


def run(build, root, *argv) -> int:
    return build.main(["--root", str(root), "release", *argv])


# --- version and matrix -----------------------------------------------------------------------------------------


def test_the_version_is_the_workspace_package_version_else_the_package_version(build, repo, tmp_path):
    ctx = build.Ctx(repo)
    assert build.release_version(ctx) == "1.2.3"
    (repo / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.9.0"\n', encoding="utf-8")
    assert build.release_version(ctx) == "0.9.0"
    (repo / "Cargo.toml").write_text('[package]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(build.Problem, match="no .workspace.package. or .package. version"):
        build.release_version(ctx)


def test_version_from_names_another_manifest_and_name_defaults_to_the_directory(build, tmp_path):
    root = tmp_path / "myrepo"
    ctx_for(build, root, '[release]\nversion_from = "crates/cli/Cargo.toml"\n')
    (root / "crates" / "cli").mkdir(parents=True)
    (root / "crates" / "cli" / "Cargo.toml").write_text('[package]\nname = "cli"\nversion = "4.5.6"\n', encoding="utf-8")
    ctx = build.Ctx(root)
    assert build.release_version(ctx) == "4.5.6"
    assert build.release_name(ctx) == "myrepo"
    assert build.archive_name(ctx, "x86_64-unknown-linux-musl", "tar.gz") == "myrepo-4.5.6-x86_64-unknown-linux-musl.tar.gz"
    with pytest.raises(build.Problem, match="not tar.gz or zip"):
        build.archive_name(ctx, "t", "rar")


def test_the_matrix_is_json_for_fromjson_from_the_configuration_alone(build, repo, capsys):
    assert run(build, repo, "matrix") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["binaries"]["include"][0] == {"target": "x86_64-unknown-linux-musl", "runner": "ubuntu-latest", "archive": "tar.gz"}
    assert {"target": "x86_64-pc-windows-msvc", "runner": "windows-latest", "archive": "zip"} in plan["binaries"]["include"]
    assert len(plan["binaries"]["include"]) == 5
    assert plan["universal"]["include"] == [{"name": "universal-apple-darwin", "runner": "macos-latest", "targets": "aarch64-apple-darwin x86_64-apple-darwin"}]
    assert plan["images"]["include"] == [{"arch": "amd64", "runner": "ubuntu-latest"}, {"arch": "arm64", "runner": "ubuntu-24.04-arm"}]
    assert run(build, repo, "matrix", "--section", "images") == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == plan["images"]


def test_without_an_image_or_universal_the_matrix_sections_are_empty(build, tmp_path, capsys):
    root = tmp_path / "r"
    ctx_for(build, root, '[[release.target]]\ntriple = "x86_64-unknown-linux-gnu"\nrunner = "ubuntu-latest"\n')
    assert run(build, root, "matrix") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["universal"]["include"] == [] and plan["images"]["include"] == []


def test_a_universal_pair_that_is_not_two_targets_is_refused(build, tmp_path, capsys):
    root = tmp_path / "r"
    ctx_for(build, root, '[release.universal]\nmacos = ["aarch64-apple-darwin"]\n')
    assert run(build, root, "matrix") == 1
    assert "names two triples" in capsys.readouterr().err
    ctx_for(build, root, '[release.universal]\nmacos = ["aarch64-apple-darwin", "x86_64-apple-darwin"]\n')
    assert run(build, root, "matrix") == 1
    assert "[[release.target]] has no aarch64-apple-darwin" in capsys.readouterr().err


# --- build -----------------------------------------------------------------------------------------------------------


def test_build_runs_cargo_per_package_with_the_target_and_packs_the_archive(build, repo, stub, capsys):
    assert run(build, repo, "build", "--target", "x86_64-unknown-linux-musl") == 0
    first, second = stub.commands
    assert first == ["cargo", "build", "--release", "--locked", "--target", "x86_64-unknown-linux-musl", "-p", "tool-cli", "--bin", "tool", "--features", "cli,tls"]
    assert second == ["cargo", "build", "--release", "--locked", "--target", "x86_64-unknown-linux-musl", "-p", "helper"]
    assert stub.envs[0]["CC_x86_64_unknown_linux_musl"] == "musl-gcc"
    assert stub.envs[0]["CARGO_TARGET_x86_64_unknown_linux_musl_LINKER"] == "musl-gcc"
    assert "RUSTFLAGS" not in stub.envs[0] or stub.envs[0]["RUSTFLAGS"] == os.environ.get("RUSTFLAGS")
    archive = repo / "dist" / "tool-1.2.3-x86_64-unknown-linux-musl.tar.gz"
    with tarfile.open(archive) as tar:
        members = {m.name: m for m in tar}
        assert sorted(members) == [
            "tool-1.2.3-x86_64-unknown-linux-musl/LICENSE",
            "tool-1.2.3-x86_64-unknown-linux-musl/README.md",
            "tool-1.2.3-x86_64-unknown-linux-musl/helper",
            "tool-1.2.3-x86_64-unknown-linux-musl/tool",
        ]
        tool = members["tool-1.2.3-x86_64-unknown-linux-musl/tool"]
        assert tool.mode == 0o755 and members["tool-1.2.3-x86_64-unknown-linux-musl/LICENSE"].mode == 0o644
        assert tar.extractfile(tool).read() == b"binary x86_64-unknown-linux-musl tool"
        assert (tool.uid, tool.gid, tool.uname, tool.mtime) == (0, 0, "", 0)
    assert "built dist/tool-1.2.3-x86_64-unknown-linux-musl.tar.gz" in capsys.readouterr().out


def test_the_same_inputs_give_the_same_archive_bytes(build, repo, stub):
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    first = (repo / "dist" / "tool-1.2.3-x86_64-unknown-linux-musl.tar.gz").read_bytes()
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    assert (repo / "dist" / "tool-1.2.3-x86_64-unknown-linux-musl.tar.gz").read_bytes() == first
    assert gzip.decompress(first)[:3] != b""


def test_a_windows_target_makes_a_zip_of_exe_files_with_the_rustflags(build, repo, stub):
    assert run(build, repo, "build", "--target", "x86_64-pc-windows-msvc") == 0
    assert stub.envs[0]["RUSTFLAGS"] == "-C target-feature=+crt-static"
    with zipfile.ZipFile(repo / "dist" / "tool-1.2.3-x86_64-pc-windows-msvc.zip") as zf:
        names = sorted(zf.namelist())
        assert names == [
            "tool-1.2.3-x86_64-pc-windows-msvc/LICENSE",
            "tool-1.2.3-x86_64-pc-windows-msvc/README.md",
            "tool-1.2.3-x86_64-pc-windows-msvc/helper.exe",
            "tool-1.2.3-x86_64-pc-windows-msvc/tool.exe",
        ]
        assert zf.read("tool-1.2.3-x86_64-pc-windows-msvc/tool.exe") == b"binary x86_64-pc-windows-msvc tool.exe"
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())


def test_sha256sums_collects_every_artifact_sorted_and_replaces_a_rebuilt_one(build, repo, stub):
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    run(build, repo, "build", "--target", "aarch64-unknown-linux-musl")
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    lines = (repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in lines] == ["tool-1.2.3-aarch64-unknown-linux-musl.tar.gz", "tool-1.2.3-x86_64-unknown-linux-musl.tar.gz"]
    for line in lines:
        digest, name = line.split("  ")
        assert digest == build.sha256((repo / "dist" / name).read_bytes())


def test_a_toolchain_setting_puts_plus_toolchain_after_cargo(build, repo, stub):
    ctx_for(build, repo, CFG.replace('name = "tool"', 'name = "tool"\ntoolchain = "1.99.1"'))
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    assert stub.commands[0][:3] == ["cargo", "+1.99.1", "build"]


def test_an_unknown_target_a_missing_binary_and_an_empty_package_list_are_problems(build, repo, stub, capsys):
    assert run(build, repo, "build", "--target", "riscv64-unknown-none") == 1
    assert "[[release.target]] has no riscv64-unknown-none" in capsys.readouterr().err
    ctx_for(build, repo, '[[release.target]]\ntriple = "x86_64-unknown-linux-gnu"\nrunner = "ubuntu-latest"\n')
    assert run(build, repo, "build", "--target", "x86_64-unknown-linux-gnu") == 1
    assert "lists nothing to build" in capsys.readouterr().err


def test_cargo_that_builds_no_binary_is_a_problem(build, repo, monkeypatch, capsys):
    monkeypatch.setattr(build, "run_tool", lambda command, env=None, cwd=None: None)
    assert run(build, repo, "build", "--target", "x86_64-unknown-linux-musl") == 1
    assert "cargo built no" in capsys.readouterr().err


def test_offline_build_uses_the_cargo_config_of_the_deps_branch_and_cuts_the_network(build, repo, stub, tmp_path, monkeypatch):
    lock = write_lock(build, repo)
    deps = tmp_path / "deps"
    vendored(build, deps / "crates", "dep-1.2.3", "a" * 64)
    vendored(build, deps / "crates", "lib-0.0.1", None, head=LIB_HEAD)
    assert run(build, repo, "build", "--target", "x86_64-unknown-linux-musl", "--offline", "--deps", str(deps), "--out", str(tmp_path / "home")) == 0
    command = stub.commands[0]
    config = command[command.index("--config") + 1]
    assert command[:2] == ["cargo", "--config"] and config.endswith("config.toml") and "cargo-home" in config
    assert Path(config).read_text(encoding="utf-8").startswith(build.CONFIG_MARK)
    assert stub.envs[0]["CARGO_NET_OFFLINE"] == "true"
    assert lock.crates, "the lock names crates the deps branch covers"


def test_offline_build_refuses_a_deps_branch_that_does_not_cover_cargo_lock(build, repo, stub, tmp_path, capsys):
    write_lock(build, repo)
    (tmp_path / "deps" / "crates").mkdir(parents=True)
    assert run(build, repo, "build", "--target", "x86_64-unknown-linux-musl", "--offline", "--deps", str(tmp_path / "deps")) == 1
    assert "run `deps sync` first" in capsys.readouterr().err
    assert stub.commands == []


# --- universal -------------------------------------------------------------------------------------------------------


def test_universal_joins_each_binary_with_lipo_checks_it_and_packs_it(build, repo, stub, capsys):
    run(build, repo, "build", "--target", "aarch64-apple-darwin")
    run(build, repo, "build", "--target", "x86_64-apple-darwin")
    stub.commands.clear()
    assert run(build, repo, "universal") == 0
    creates = [c for c in stub.commands if c[:2] == ["lipo", "-create"]]
    assert len(creates) == 2
    assert creates[0][4:] == [str(repo / "target" / t / "release" / "tool") for t in ("aarch64-apple-darwin", "x86_64-apple-darwin")]
    assert [c[:2] for c in stub.captured] == [["lipo", "-info"], ["lipo", "-info"]]
    archive = repo / "dist" / "tool-1.2.3-universal-apple-darwin.tar.gz"
    with tarfile.open(archive) as tar:
        data = tar.extractfile("tool-1.2.3-universal-apple-darwin/tool").read()
        assert data.startswith(b"fat binary aarch64-apple-darwin tool+binary x86_64-apple-darwin tool")
    assert "tool-1.2.3-universal-apple-darwin.tar.gz" in (repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8")
    assert "built dist/tool-1.2.3-universal-apple-darwin.tar.gz" in capsys.readouterr().out


def test_universal_needs_both_builds_and_a_binary_lipo_says_has_both_architectures(build, repo, stub, capsys):
    run(build, repo, "build", "--target", "aarch64-apple-darwin")
    assert run(build, repo, "universal") == 1
    assert "x86_64-apple-darwin" in capsys.readouterr().err
    run(build, repo, "build", "--target", "x86_64-apple-darwin")
    stub.info = "Non-fat file: out is architecture: arm64"
    assert run(build, repo, "universal") == 1
    assert "does not list both architectures" in capsys.readouterr().err


def test_universal_without_two_triples_is_a_problem(build, tmp_path, capsys):
    root = tmp_path / "r"
    ctx_for(build, root)
    assert run(build, root, "universal") == 1
    assert "must name two triples" in capsys.readouterr().err


# --- image -----------------------------------------------------------------------------------------------------------


def test_image_builds_natively_and_saves_an_oci_archive_with_podman(build, repo, stub, monkeypatch, capsys):
    monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "podman" else None)
    assert run(build, repo, "image", "--arch", "arm64") == 0
    built, saved = stub.commands
    assert built == ["podman", "build", "--platform", "linux/arm64", "-f", str(repo / "packaging/Containerfile"), "-t", "tool:1.2.3-arm64", str(repo / ".")]
    out = repo / "dist" / "tool-1.2.3-image-arm64.tar"
    assert saved == ["podman", "save", "--format", "oci-archive", "-o", str(out), "tool:1.2.3-arm64"]
    assert "tool-1.2.3-image-arm64.tar" in (repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8")


def test_image_falls_back_to_docker_which_has_no_oci_archive_and_the_tool_is_a_setting(build, repo, stub, monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    assert run(build, repo, "image", "--arch", "amd64") == 0
    assert stub.commands[1] == ["docker", "save", "-o", str(repo / "dist" / "tool-1.2.3-image-amd64.tar"), "tool:1.2.3-amd64"]
    ctx_for(build, repo, CFG.replace('file = "packaging/Containerfile"', 'file = "packaging/Containerfile"\ntool = "podman"\nname = "registry.example/tool"'))
    stub.commands.clear()
    assert run(build, repo, "image", "--arch", "amd64") == 0
    assert stub.commands[0][0] == "podman" and "registry.example/tool:1.2.3-amd64" in stub.commands[0]


def test_image_problems_name_the_cause(build, repo, stub, monkeypatch, capsys):
    assert run(build, repo, "image", "--arch", "riscv") == 1
    assert "archs has no riscv; it has amd64, arm64" in capsys.readouterr().err
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    assert run(build, repo, "image", "--arch", "amd64") == 1
    assert "neither podman nor docker" in capsys.readouterr().err
    ctx_for(build, repo, "")
    assert run(build, repo, "image", "--arch", "amd64") == 1
    assert "[release.image] file is not set" in capsys.readouterr().err


# --- verify ----------------------------------------------------------------------------------------------------------


def test_verify_passes_when_file_says_statically_linked(build, repo, stub, monkeypatch, capsys):
    monkeypatch.setattr(build.shutil, "which", lambda name: "/usr/bin/file")
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    run(build, repo, "build", "--target", "aarch64-unknown-linux-musl")
    capsys.readouterr()
    assert run(build, repo, "verify") == 0
    assert "ok: 2 Linux target(s) statically linked" in capsys.readouterr().out
    assert run(build, repo, "verify", "--target", "x86_64-unknown-linux-musl") == 0


def test_verify_names_a_dynamic_binary_and_a_missing_archive(build, repo, stub, monkeypatch, capsys):
    monkeypatch.setattr(build.shutil, "which", lambda name: "/usr/bin/file")
    run(build, repo, "build", "--target", "x86_64-unknown-linux-musl")
    monkeypatch.setattr(build, "capture_tool", lambda command, env=None: (0, "ELF 64-bit LSB pie executable, dynamically linked"))
    capsys.readouterr()
    assert run(build, repo, "verify") == 1
    err = capsys.readouterr().err
    assert "x86_64-unknown-linux-musl: tool is not statically linked: ELF 64-bit LSB pie executable, dynamically linked" in err
    assert "helper is not statically linked" in err
    assert "tool-1.2.3-aarch64-unknown-linux-musl.tar.gz: missing; build aarch64-unknown-linux-musl first" in err


def test_verify_needs_the_file_command_and_a_linux_target(build, repo, stub, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    assert run(build, repo, "verify") == 1
    assert "`file` command is not installed" in capsys.readouterr().err
    root = tmp_path / "mac"
    ctx_for(build, root, '[[release.target]]\ntriple = "aarch64-apple-darwin"\nrunner = "macos-latest"\n')
    assert run(build, root, "verify") == 0
    assert "no Linux target to verify" in capsys.readouterr().out
    assert run(build, repo, "verify", "--target", "nonsense") == 1
