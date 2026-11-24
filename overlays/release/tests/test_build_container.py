"""`build.py container`: the .env layers, what is refused, and the engine command line (no engine is run)."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from conftest import REPO_TOOLS, commit_all, git, init_repo
from test_build_deps import LIB_HEAD, LOCK, ctx_for, vendored

RUSTUP_X64 = "a" * 64
RUSTUP_ARM64 = "b" * 64
RUST_DIGEST = "c" * 64
DEBIAN_DIGEST = "d" * 64
DEPS_COMMIT = "e" * 40
OTHER_DIGEST = "f" * 64

CFG = """
[sdk]
lock = "tools/sdk.lock"
groups = ["core", "release", "deps"]

[release]
name = "tool"

[release.image]
file = "packaging/container/Containerfile"

[container]
base_item = "release rust-image"
runtime_item = "release debian-image"
"""

SDK_LOCK = f"""# lock
core rustup-init 1.29.2 linux-x64 {RUSTUP_X64} https://static.example.com/rustup/archive/1.29.2/x86_64-unknown-linux-gnu/rustup-init
core rustup-init 1.29.2 linux-arm64 {RUSTUP_ARM64} https://static.example.com/rustup/archive/1.29.2/aarch64-unknown-linux-gnu/rustup-init
core rust 1.100.0 any - -
core rust 1.89.0 any - -
release rust-image 1.89-bookworm any {RUST_DIGEST} docker://docker.io/library/rust
release debian-image bookworm-slim any {DEBIAN_DIGEST} docker://docker.io/library/debian
deps crates {DEPS_COMMIT} any - https://example.com/org/tool.git
"""

BASE_DEFAULT = f"docker.io/library/rust:1.89-bookworm@sha256:{RUST_DIGEST}"
PINNED_OTHER = f"registry.example.com/rust:1.89@sha256:{OTHER_DIGEST}"


@pytest.fixture(autouse=True)
def clean_environment(build, monkeypatch):
    for key in build.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def repo(build, tmp_path):
    root = init_repo(tmp_path / "repo")
    ctx_for(build, root, CFG)
    (root / "Cargo.toml").write_text('[package]\nname = "tool"\nversion = "1.2.3"\n', encoding="utf-8")
    (root / "Cargo.lock").write_text(LOCK, encoding="utf-8")
    (root / "tools" / "sdk.lock").write_text(SDK_LOCK, encoding="utf-8")
    commit_all(root)
    return root


def deps_checkout(build, base: Path) -> Path:
    """A deps checkout that covers LOCK."""
    crates = base / "crates"
    vendored(build, crates, "dep-1.2.3", "a" * 64)
    vendored(build, crates, "lib-0.0.1", None, head=LIB_HEAD)
    return base


@pytest.fixture
def deps(build, tmp_path):
    return deps_checkout(build, tmp_path / "deps")


def container(build, root, capsys, *argv, ok: int = 0) -> str:
    capsys.readouterr()
    assert build.main(["--root", str(root), "container", *argv]) == ok
    out = capsys.readouterr()
    return out.out if ok == 0 else out.err


def plan(build, root, capsys, *argv, stage: str = "build") -> dict:
    return json.loads(container(build, root, capsys, stage, "--print", "--json", "--tag", "tool:t", *argv))


def args_of(command: list[str], flag: str) -> list[str]:
    return [command[i + 1] for i, word in enumerate(command) if word == flag]


def write_env(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


# --- the layers ---------------------------------------------------------------------------------------------


def test_each_layer_wins_over_the_one_before_it(build, repo, deps, capsys, monkeypatch):
    write_env(repo, ".env", f"DEPS_DIR={deps}\nNO_PROXY=from-env-file\nAPT_MIRROR=https://env.example.com/debian\nCONTAINER_PLATFORM=linux/amd64\n")
    write_env(repo, ".env.local", "NO_PROXY=from-local-file\nAPT_MIRROR=https://local.example.com/debian\n")
    monkeypatch.setenv("APT_MIRROR", "https://process.example.com/debian")
    report = plan(build, repo, capsys, "--env", "APT_MIRROR=https://flag.example.com/debian")
    where = {row["key"]: (row["value"], row["source"]) for row in report["non_default"]}
    assert where["APT_MIRROR"] == ("https://flag.example.com/debian", "--env")
    assert where["NO_PROXY"] == ("from-local-file", ".env.local")
    assert where["CONTAINER_PLATFORM"] == ("linux/amd64", ".env")
    assert args_of(report["command"], "--build-arg") == ["APT_MIRROR=https://flag.example.com/debian", "NO_PROXY=from-local-file"]
    assert args_of(report["command"], "--platform") == ["linux/amd64"]
    monkeypatch.delenv("APT_MIRROR")
    report = plan(build, repo, capsys)
    assert {row["key"]: row["value"] for row in report["non_default"]}["APT_MIRROR"] == "https://local.example.com/debian"


def test_an_empty_value_clears_a_key_set_by_an_earlier_layer(build, repo, deps, capsys):
    write_env(repo, ".env", f"DEPS_DIR={deps}\nNO_PROXY=internal.example.com\n")
    write_env(repo, ".env.local", "NO_PROXY=\n")
    report = plan(build, repo, capsys)
    assert "NO_PROXY" not in {row["key"] for row in report["non_default"]}
    assert "--build-arg" not in report["command"]


def test_the_environment_is_read_for_known_keys_only(build, repo, deps, capsys, monkeypatch):
    monkeypatch.setenv("PATH_OF_SOMETHING_ELSE", "x")
    monkeypatch.setenv("DEPS_DIR", str(deps))
    assert plan(build, repo, capsys)["deps"]["dir"] == str(deps.resolve())


def test_an_env_file_reads_comments_quotes_and_export(build):
    rows, errors = build.parse_env_text(
        "# a comment\n\nexport NO_PROXY=a.example.com # why\nAPT_MIRROR='https://m.example.com/debian'\nDEPS_REF=\"abc # not a comment\"\n", ".env"
    )
    assert errors == []
    assert [(key, value) for _, key, value in rows] == [("NO_PROXY", "a.example.com"), ("APT_MIRROR", "https://m.example.com/debian"), ("DEPS_REF", "abc # not a comment")]
    assert build.parse_env_text("NOT A PAIR\n9X=1\n", ".env")[1] == [".env:1: not KEY=VALUE", ".env:2: not KEY=VALUE"]


# --- refused ---------------------------------------------------------------------------------------------------


def test_an_unknown_key_is_refused_naming_the_key_and_the_file(build, repo, capsys):
    write_env(repo, ".env", "NO_PROXY=x\n")
    write_env(repo, ".env.local", "# keep\nRUSTUP_DIST_SERVR=https://m.example.com\n")
    err = container(build, repo, capsys, "build", "--print", ok=1)
    assert ".env.local:2: unknown key RUSTUP_DIST_SERVR" in err
    (repo / ".env.local").unlink()
    (repo / ".env").unlink()
    err = container(build, repo, capsys, "build", "--print", "--env", "NOPE=1", "--env", "bare", ok=1)
    assert "--env NOPE: unknown key" in err and "--env bare: not KEY=VALUE" in err


def test_an_unpinned_image_is_refused_unless_allowed_and_then_reported(build, repo, deps, capsys):
    write_env(repo, ".env", f"DEPS_DIR={deps}\nBASE_IMAGE=docker.io/library/rust:1.89-bookworm\n")
    err = container(build, repo, capsys, "build", "--print", ok=1)
    assert "BASE_IMAGE (.env): docker.io/library/rust:1.89-bookworm has no @sha256: digest" in err
    assert container(build, repo, capsys, "build", "--print", "--env", "RUNTIME_IMAGE=debian:bookworm", ok=1).count("has no @sha256: digest") == 2
    out = container(build, repo, capsys, "build", "--print", "--env", "ALLOW_UNPINNED_IMAGE=1", "--env", "RUNTIME_IMAGE=debian:bookworm")
    assert "note: BASE_IMAGE has no digest (ALLOW_UNPINNED_IMAGE=1): the build is not reproducible" in out
    assert "note: RUNTIME_IMAGE has no digest" in out
    assert container(build, repo, capsys, "build", "--print", "--env", "ALLOW_UNPINNED_IMAGE=yes", ok=1).count("must be 1") == 1


def test_a_pinned_image_of_another_registry_is_a_non_default_not_an_error(build, repo, deps, capsys):
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", f"BASE_IMAGE={PINNED_OTHER}")
    assert {row["key"] for row in report["non_default"]} == {"DEPS_DIR", "BASE_IMAGE"}
    assert f"BASE_IMAGE={PINNED_OTHER}" in args_of(report["command"], "--build-arg")


def test_the_lock_s_own_image_is_not_reported_as_a_non_default(build, repo, deps, capsys):
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", f"BASE_IMAGE={BASE_DEFAULT}")
    assert {row["key"] for row in report["non_default"]} == {"DEPS_DIR"}


def test_a_rustup_version_and_its_hashes_go_together_and_a_mirror_never_replaces_them(build, repo, deps, capsys):
    mirror = "https://mirror.example.com/rustup"
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", f"RUSTUP_INIT_BASE_URL={mirror}")
    assert args_of(report["command"], "--build-arg") == [f"RUSTUP_INIT_BASE_URL={mirror}"]
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", "--env", f"RUSTUP_INIT_SHA256_X86_64={'1' * 64}", ok=1)
    assert "RUSTUP_INIT_SHA256_X86_64 given without RUSTUP_INIT_SHA256_AARCH64, RUSTUP_VERSION" in err
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", "--env", "RUSTUP_VERSION=1.30", ok=1)
    assert "RUSTUP_VERSION (--env): not an exact version (MAJOR.MINOR.PATCH): 1.30" in err
    report = plan(
        build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", "RUSTUP_VERSION=1.30.0",
        "--env", f"RUSTUP_INIT_SHA256_X86_64={'1' * 64}", "--env", f"RUSTUP_INIT_SHA256_AARCH64={'2' * 64}",
    )  # fmt: skip
    assert {"RUSTUP_VERSION", "RUSTUP_INIT_SHA256_X86_64", "RUSTUP_INIT_SHA256_AARCH64"} <= {row["key"] for row in report["non_default"]}


@pytest.mark.parametrize(
    "setting,message",
    [
        ("RUST_VERSION=stable", "RUST_VERSION (--env): not an exact version or nightly-YYYY-MM-DD: stable"),
        ("RUSTUP_DIST_SERVER=mirror.example.com", "RUSTUP_DIST_SERVER (--env): not a URL"),
        ("CONTAINER_ENGINE=nerdctl", "CONTAINER_ENGINE (--env): must be podman or docker: nerdctl"),
        ("EXTRA_CA_CERT=no-such-file.pem", "EXTRA_CA_CERT (--env): no-such-file.pem is not a file"),
        ("DEPS_DIR=no-such-dir", "DEPS_DIR (--env): no-such-dir is not a directory"),
    ],
)
def test_a_value_that_cannot_be_right_is_refused_naming_the_key(build, repo, capsys, setting, message):
    assert message in container(build, repo, capsys, "build", "--print", "--env", setting, ok=1)


# --- secrets ------------------------------------------------------------------------------------------------------


def test_a_proxy_with_credentials_is_on_no_command_line_and_in_no_output(build, repo, deps, capsys, monkeypatch):
    secret = "http://builder:hunter2@proxy.example.com:3128"
    monkeypatch.setenv("HTTPS_PROXY", secret)
    monkeypatch.setenv("DEPS_DIR", str(deps))
    text = container(build, repo, capsys, "build", "--print")
    as_json = container(build, repo, capsys, "build", "--print", "--json")
    for shown in (text, as_json):
        assert "hunter2" not in shown and "builder" not in shown
    assert "non-default: HTTPS_PROXY=http://***@proxy.example.com:3128 (environment)" in text
    assert "passed by name through the environment: HTTPS_PROXY" in text
    command = json.loads(as_json)["command"]
    assert args_of(command, "--build-arg") == ["HTTPS_PROXY"]
    assert json.loads(as_json)["env"] == ["HTTPS_PROXY"]


def test_a_mirror_url_with_credentials_is_passed_by_name_and_hidden(build, repo, deps, capsys, monkeypatch):
    monkeypatch.setenv("RUSTUP_DIST_SERVER", "https://u:tok3n@mirror.example.com")
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}")
    assert args_of(report["command"], "--build-arg") == ["RUSTUP_DIST_SERVER"]
    assert "tok3n" not in json.dumps(report)


def test_a_run_hands_the_secret_to_the_engine_through_its_environment_only(build, repo, deps, monkeypatch):
    seen = {}
    monkeypatch.setattr(build, "run_tool", lambda command, env=None, cwd=None: seen.update(command=command, env=env, cwd=cwd))
    monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("HTTP_PROXY", "http://builder:hunter2@proxy.example.com:3128")
    assert build.main(["--root", str(repo), "container", "build", "--tag", "tool:t", "--env", f"DEPS_DIR={deps}"]) == 0
    assert seen["env"]["HTTP_PROXY"] == "http://builder:hunter2@proxy.example.com:3128"
    assert not any("hunter2" in word for word in seen["command"])
    assert seen["cwd"] == repo.resolve()


def test_a_failed_fetch_does_not_echo_the_credentials_of_its_url(build, repo, capsys, tmp_path):
    err = container(build, repo, capsys, "build", "--env", f"DEPS_GIT_URL=file://u:pw@{tmp_path.as_posix()}/missing", "--env", f"DEPS_REF={'1' * 40}", ok=1)
    assert "pw@" not in err and "***@" in err


# --- the engine command line ------------------------------------------------------------------------------------


def expected(
    root: Path, deps: Path, engine: str = "podman", target: bool = False, args: tuple[str, ...] = (), tag: str = "tool:t", extra: tuple[str, ...] = (), keys: str = "DEPS_DIR"
) -> list[str]:
    head = git(root, "rev-parse", "HEAD")
    return [
        engine, "build", "--file", str(root / "packaging/container/Containerfile"), *(["--target", "test"] if target else []), "--tag", tag,
        *extra, "--build-context", f"deps={deps.resolve()}", "--build-context", f"ca={root / 'target/container/ca-empty'}", *args,
        "--label", f"build.py.deps-commit={'unknown'}", "--label", f"org.opencontainers.image.revision={head}",
        "--label", f"build.py.non-default={keys}", str(root.resolve()),
    ]  # fmt: skip


def test_podman_gets_the_build_context_one_argument_for_each_key_and_the_labels(build, repo, deps, capsys):
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}")
    assert report["engine"] == "podman"
    assert report["command"] == expected(repo, deps)
    assert report["deps"] == {"dir": str(deps.resolve()), "commit": "unknown"}


def test_the_test_stage_names_its_target_and_its_tag(build, repo, deps, capsys):
    report = json.loads(container(build, repo, capsys, "test", "--print", "--json", "--env", f"DEPS_DIR={deps}"))
    assert report["command"] == expected(repo, deps, target=True, tag="tool-test:1.2.3")
    report = json.loads(container(build, repo, capsys, "build", "--print", "--json", "--env", f"DEPS_DIR={deps}", "--target", "runtime"))
    assert report["command"][report["command"].index("--target") + 1] == "runtime"
    assert report["command"][report["command"].index("--tag") + 1] == "tool:1.2.3"


def test_docker_gets_the_same_arguments_and_no_build_arg_file(build, repo, deps, capsys):
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", "CONTAINER_ENGINE=docker", "--env", "CONTAINER_PLATFORM=linux/arm64", "--env", "NO_PROXY=a.example.com")
    command = report["command"]
    assert command[:2] == ["docker", "build"] and "--build-arg-file" not in command
    assert command == expected(repo, deps, engine="docker", args=("--build-arg", "NO_PROXY=a.example.com"), extra=("--platform", "linux/arm64"), keys="DEPS_DIR,NO_PROXY,CONTAINER_ENGINE,CONTAINER_PLATFORM")
    assert args_of(command, "--build-context") == [f"deps={deps.resolve()}", f"ca={repo / 'target/container/ca-empty'}"]
    assert args_of(command, "--platform") == ["linux/arm64"]


def test_the_engine_defaults_to_the_one_found_then_to_podman(build, repo, deps, capsys, monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    assert plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}")["engine"] == "docker"
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    assert plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}")["engine"] == "podman"
    ctx_for(build, repo, CFG.replace("[release]", '[release]\n').replace('file = "packaging/container/Containerfile"', 'file = "packaging/container/Containerfile"\ntool = "docker"'))
    assert plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}")["engine"] == "docker"


def test_a_run_without_the_engine_is_a_problem_and_print_needs_none(build, repo, deps, capsys, monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.setattr(build, "run_tool", lambda *a, **k: pytest.fail("no engine may run"))
    assert "podman is not installed" in container(build, repo, capsys, "build", "--tag", "t:1", "--env", f"DEPS_DIR={deps}", ok=1)
    container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}")


def test_a_ca_file_is_a_build_context_of_its_own_directory_and_a_build_argument_by_name(build, repo, deps, capsys, tmp_path):
    pem = tmp_path / "certs" / "corp.pem"
    pem.parent.mkdir()
    pem.write_text("not a real certificate\n", encoding="utf-8")
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", f"EXTRA_CA_CERT={pem}")
    assert f"ca={pem.parent.resolve()}" in args_of(report["command"], "--build-context")
    assert args_of(report["command"], "--build-arg") == ["EXTRA_CA_CERT=corp.pem"]


def test_the_labels_name_each_non_default_key_and_never_a_value(build, repo, deps, capsys):
    report = plan(build, repo, capsys, "--env", f"DEPS_DIR={deps}", "--env", "NO_PROXY=a.example.com", "--env", "APT_MIRROR=https://m.example.com/debian")
    labels = dict(label.split("=", 1) for label in args_of(report["command"], "--label"))
    assert set(labels["build.py.non-default"].split(",")) == {"DEPS_DIR", "APT_MIRROR", "NO_PROXY"}
    assert "m.example.com" not in " ".join(args_of(report["command"], "--label"))
    assert labels["org.opencontainers.image.revision"] == git(repo, "rev-parse", "HEAD")


def test_the_non_defaults_are_printed_before_the_command(build, repo, deps, capsys):
    out = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", "--env", "NO_PROXY=a.example.com")
    lines = out.splitlines()
    assert lines[0].startswith("non-default: DEPS_DIR=") and lines[1] == "non-default: NO_PROXY=a.example.com (--env)"
    assert lines[-1].startswith("podman build --file ")
    none = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", "--env", f"BASE_IMAGE={BASE_DEFAULT}").splitlines()
    assert none[0].startswith("non-default: DEPS_DIR=") and not any("BASE_IMAGE=" in line.split("(")[0] for line in none[:2])


# --- the deps checkout ----------------------------------------------------------------------------------------


def test_without_an_override_print_names_the_commit_and_url_it_would_fetch_and_fetches_nothing(build, repo, capsys):
    out = container(build, repo, capsys, "build", "--print")
    assert f"note: deps {DEPS_COMMIT[:12]} is fetched from https://example.com/org/tool.git when the build runs" in out
    assert "non-default: none" in out
    assert not (repo / "target").exists()


def test_a_deps_dir_that_does_not_cover_cargo_lock_is_refused(build, repo, deps, capsys, tmp_path):
    (deps / "crates" / "dep-1.2.3" / "Cargo.toml").write_bytes(b"[package] # edited\n")
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", ok=1)
    assert "DEPS_DIR (--env): crates/dep-1.2.3/Cargo.toml: SHA-256" in err
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={tmp_path}", ok=1)
    assert "DEPS_DIR (--env): " in err


def test_deps_dir_excludes_the_fetch_settings(build, repo, deps, capsys):
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}", "--env", f"DEPS_REF={'1' * 40}", "--env", "DEPS_GIT_URL=https://example.com/x.git", ok=1)
    assert "DEPS_DIR and DEPS_GIT_URL are exclusive" in err and "DEPS_DIR and DEPS_REF are exclusive" in err


def deps_commit(build, repo, deps: Path, with_crates: bool = True) -> str:
    """A commit of the repository's own object store holding the deps checkout's files."""
    from test_build_deps import make_tree_commit

    entries = {}
    if with_crates:
        for path in sorted(p for p in deps.rglob("*") if p.is_file()):
            entries[path.relative_to(deps).as_posix()] = ("100644", path.read_bytes())
    else:
        entries["README.txt"] = ("100644", b"nothing vendored\n")
    return make_tree_commit(repo, entries)


def test_a_deps_ref_that_is_not_a_deps_branch_is_refused_and_a_good_one_is_reported(build, repo, deps, capsys):
    bad = deps_commit(build, repo, deps, with_crates=False)
    err = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_REF={bad}", ok=1)
    assert "DEPS_REF: deps at " in err and "crates/dep-1.2.3: missing" in err
    good = deps_commit(build, repo, deps)
    out = container(build, repo, capsys, "build", "--print", "--env", f"DEPS_REF={good}")
    assert f"non-default: DEPS_REF={good} (--env)" in out
    assert f"deps: {repo / 'target/container'}" in out and good in out


def test_a_missing_deps_row_is_a_problem_unless_deps_dir_names_a_checkout(build, repo, deps, capsys):
    (repo / "tools" / "sdk.lock").write_text(SDK_LOCK.replace(f"deps crates {DEPS_COMMIT} any - https://example.com/org/tool.git\n", ""), encoding="utf-8")
    assert "expected one `deps crates` row" in container(build, repo, capsys, "build", "--print", ok=1)
    container(build, repo, capsys, "build", "--print", "--env", f"DEPS_DIR={deps}")


def test_a_real_run_fetches_the_commit_checks_it_and_hands_the_engine_the_checkout(build, repo, deps, capsys, monkeypatch, tmp_path):
    source = init_repo(tmp_path / "deps-source")
    deps_checkout(build, source)
    commit = commit_all(source)
    ran = []
    monkeypatch.setattr(build, "run_tool", lambda command, env=None, cwd=None: ran.append(command))
    monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = ["--root", str(repo), "container", "build", "--tag", "tool:t", "--env", f"DEPS_GIT_URL={source}", "--env", f"DEPS_REF={commit}"]
    assert build.main(argv) == 0
    (command,) = ran
    deps_dir = Path(args_of(command, "--build-context")[0].removeprefix("deps="))
    assert deps_dir.parent == repo / "target" / "container" and (deps_dir / "crates" / "dep-1.2.3" / "Cargo.toml").is_file()
    assert git(deps_dir, "rev-parse", "HEAD") == commit
    assert (repo / "target" / "container" / "ca-empty").is_dir()
    assert f"build.py.deps-commit={commit}" in args_of(command, "--label")
    ran.clear()
    assert build.main(argv) == 0 and len(ran) == 1
    (source / "crates" / "dep-1.2.3" / "Cargo.toml").write_bytes(b"[package] # edited\n")
    changed = commit_all(source)
    capsys.readouterr()
    assert build.main([*argv[:-1], f"DEPS_REF={changed}"]) == 1
    assert "SHA-256" in capsys.readouterr().err
    assert len(ran) == 1


def test_a_fetch_that_gives_another_commit_than_the_one_asked_for_is_refused(build, repo, capsys, monkeypatch):
    monkeypatch.setattr(build, "run_git", lambda *args, cwd=None, binary=False: "9" * 40 if args[:1] == ("rev-parse",) else "")
    monkeypatch.setattr(build, "git_ok", lambda *args, cwd=None: True)
    err = container(build, repo, capsys, "build", "--tag", "t:1", ok=1)
    assert f"fetching {DEPS_COMMIT} gave {'9' * 40}" in err


# --- the surface --------------------------------------------------------------------------------------------------


def test_help_env_lists_every_key_and_the_surface_lists_the_container_commands(build, capsys):
    assert build.main(["help", "env"]) == 0
    text = capsys.readouterr().out
    assert all(f"  {key}  (" in text for key in build.ENV_KEYS)
    assert build.main(["help"]) == 0
    surface = capsys.readouterr().out
    assert "build.py container" in surface and "container build" in surface


def test_container_without_a_containerfile_setting_says_so(build, tmp_path, capsys):
    root = tmp_path / "bare"
    ctx_for(build, root, "")
    assert container(build, root, capsys, "build", "--print", ok=1).strip().endswith("[container] file (or [release.image] file) is not set")


# --- the template and its rules -----------------------------------------------------------------------------------

TEMPLATE = REPO_TOOLS.parent / "packaging" / "container" / "Containerfile.rust"
ENV_EXAMPLE = REPO_TOOLS.parent / ".env.example"
BUILD_MD = REPO_TOOLS.parents[1] / "kb" / "BUILD.md"
PREDEFINED = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}


def template_arg(name: str) -> str:
    return re.search(rf"^ARG {name}=(\S+)$", TEMPLATE.read_text(encoding="utf-8"), re.MULTILINE).group(1)


def documented_rules() -> str:
    """The [container] and [[sdk.agree]] block BUILD.md tells a repository to copy."""
    text = BUILD_MD.read_text(encoding="utf-8")
    block = re.search(r"## Container builds.*?```toml\n(.*?)```", text, re.DOTALL).group(1)
    assert block.count("[[sdk.agree]]") == 8
    return block


def template_repo(build, tmp_path) -> Path:
    """A repository holding the template as its Containerfile and a lock made of the template's own pins."""
    root = init_repo(tmp_path / "t")
    cfg = f'[sdk]\ngroups = ["core", "release", "deps"]\nall_platforms = []\n[release]\nname = "tool"\n[release.image]\nfile = "packaging/container/Containerfile"\n{documented_rules()}'
    ctx_for(build, root, cfg)
    container_dir = root / "packaging" / "container"
    container_dir.mkdir(parents=True)
    (container_dir / "Containerfile").write_bytes(TEMPLATE.read_bytes())
    rust, digest = template_arg("BASE_IMAGE").removeprefix("docker.io/library/rust:").split("@sha256:")
    debian, runtime_digest = template_arg("RUNTIME_IMAGE").removeprefix("docker.io/library/debian:").split("@sha256:")
    version = template_arg("RUSTUP_VERSION")
    rows = [
        f"core rustup-init {version} linux-x64 {template_arg('RUSTUP_INIT_SHA256_X86_64')} https://static.example.com/x86_64/rustup-init",
        f"core rustup-init {version} linux-arm64 {template_arg('RUSTUP_INIT_SHA256_AARCH64')} https://static.example.com/aarch64/rustup-init",
        f"core rust {template_arg('RUST_VERSION')} any - -",
        f"release rust-image {rust} any {digest} docker://docker.io/library/rust",
        f"release debian-image {debian} any {runtime_digest} docker://docker.io/library/debian",
    ]
    (root / "tools" / "sdk.lock").write_text("# lock\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return root


def check_sdk(build, root, capsys) -> tuple[int, str]:
    capsys.readouterr()
    code = build.main(["--root", str(root), "sdk", "check"])
    return code, capsys.readouterr().err


def test_the_template_has_the_stages_and_every_build_argument_of_the_catalogue(build):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.findall(r"^FROM \S+ AS (\w+)$", text, re.MULTILINE) == ["sdk", "build", "test", "runtime"]
    declared = set(re.findall(r"^ARG ([A-Z0-9_]+)(?:=|$)", text, re.MULTILINE))
    wanted = {key for key, spec in build.ENV_KEYS.items() if spec.arg} - PREDEFINED
    assert wanted <= declared, sorted(wanted - declared)
    assert declared <= set(build.ENV_KEYS) | {"BASE_IMAGE", "RUNTIME_IMAGE"}, sorted(declared - set(build.ENV_KEYS))


def test_the_build_and_test_stages_run_every_step_with_no_network_and_the_sdk_stage_checks_the_hash(build):
    text = TEMPLATE.read_text(encoding="utf-8")
    stages = {m.group(1): m.group(0) for m in re.finditer(r"^FROM \S+ AS (\w+)$.*?(?=^FROM |\Z)", text, re.MULTILINE | re.DOTALL)}
    for name in ("build", "test"):
        runs = re.findall(r"^RUN (.*?)(?=^[A-Z]+ |\Z)", stages[name], re.MULTILINE | re.DOTALL)
        assert runs and all(run.startswith("--network=none ") for run in runs), name
    assert "sha256sum --check --strict" in stages["sdk"] and "--network=none" not in stages["sdk"]
    assert "COPY --from=deps /crates /deps/crates" in stages["build"]
    assert "python3 tools/build.py offline plan --deps /deps --out /opt/offline" in stages["build"]
    assert "cargo --config /opt/offline/cargo-home/config.toml build --release --locked --offline" in stages["build"]
    assert "cargo --config /opt/offline/cargo-home/config.toml test --workspace --all-features --locked --offline" in stages["test"]
    assert "ARG " not in stages["runtime"]


def test_the_env_example_documents_every_key_with_its_default_commented_out(build):
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^#([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    assert documented == set(build.ENV_KEYS)
    assert build.parse_env_text(text, ".env.example") == ([], [])


def test_the_documented_rules_hold_the_template_to_the_lock_and_fail_when_either_changes(build, tmp_path, capsys):
    root = template_repo(build, tmp_path)
    assert check_sdk(build, root, capsys)[0] == 0
    containerfile = root / "packaging" / "container" / "Containerfile"
    original = containerfile.read_text(encoding="utf-8")
    zeros = "0" * 64
    mutations = {
        "base image digest": (f"rust:1.89-bookworm@sha256:{zeros}", f"rust:1.89-bookworm@sha256:{'1' * 64}"),
        "base image tag": ("rust:1.89-bookworm@", "rust:1.90-bookworm@"),
        "runtime digest": (f"bookworm-slim@sha256:{zeros}", f"bookworm-slim@sha256:{'2' * 64}"),
        "runtime tag": ("debian:bookworm-slim@", "debian:trixie-slim@"),
        "toolchain": ("ARG RUST_VERSION=1.100.0", "ARG RUST_VERSION=1.101.0"),
        "rustup version": ("ARG RUSTUP_VERSION=1.29.2", "ARG RUSTUP_VERSION=1.30.0"),
        "x86_64 hash": (f"ARG RUSTUP_INIT_SHA256_X86_64={zeros}", f"ARG RUSTUP_INIT_SHA256_X86_64={'3' * 64}"),
        "aarch64 hash": (f"ARG RUSTUP_INIT_SHA256_AARCH64={zeros}", f"ARG RUSTUP_INIT_SHA256_AARCH64={'4' * 64}"),
    }
    for label, (old, new) in mutations.items():
        assert original.count(old) == 1, label
        containerfile.write_text(original.replace(old, new), encoding="utf-8")
        code, err = check_sdk(build, root, capsys)
        assert code == 1 and "Containerfile" in err, label
    containerfile.write_text(original, encoding="utf-8")
    assert check_sdk(build, root, capsys)[0] == 0
    lock = (root / "tools" / "sdk.lock").read_text(encoding="utf-8")
    (root / "tools" / "sdk.lock").write_text(lock.replace("core rust 1.100.0 any", "core rust 1.102.0 any"), encoding="utf-8")
    code, err = check_sdk(build, root, capsys)
    assert code == 1 and "1.102.0" in err


def test_the_template_in_a_fixture_repository_prints_the_build_with_the_context_and_no_non_default(build, tmp_path, capsys, deps):
    root = template_repo(build, tmp_path)
    (root / "Cargo.toml").write_text('[package]\nname = "tool"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "Cargo.lock").write_text(LOCK, encoding="utf-8")
    commit_all(root)
    out = container(build, root, capsys, "test", "--print", "--env", f"DEPS_DIR={deps}", "--env", f"BASE_IMAGE={template_arg('BASE_IMAGE')}")
    lines = out.splitlines()
    assert lines[0].startswith("non-default: DEPS_DIR=")
    assert "BASE_IMAGE=" not in lines[0] and not lines[1].startswith("non-default: BASE_IMAGE")
    command = [word.strip("'") for word in shlex.split(lines[-1], posix=False)]
    assert command[:8] == ["podman", "build", "--file", str(root / "packaging/container/Containerfile"), "--target", "test", "--tag", "tool-test:0.1.0"]
    assert f"deps={deps.resolve()}" in command and any(word.startswith("ca=") for word in command)


# --- release image through the container build ---------------------------------------------------------------------


def test_release_image_from_deps_builds_with_the_deps_context_for_the_arch_and_saves_the_archive(build, repo, deps, monkeypatch):
    ctx_for(build, repo, CFG.replace('file = "packaging/container/Containerfile"', 'file = "packaging/container/Containerfile"\nfrom_deps = true\narchs = { amd64 = "x", arm64 = "y" }'))
    ran = []

    def run(command, env=None, cwd=None):
        ran.append(command)
        if "save" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"archive")

    monkeypatch.setattr(build, "run_tool", run)
    monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "podman" else None)
    monkeypatch.setenv("DEPS_DIR", str(deps))
    assert build.main(["--root", str(repo), "release", "image", "--arch", "arm64"]) == 0
    built, saved = ran
    assert built[:2] == ["podman", "build"] and args_of(built, "--platform") == ["linux/arm64"] and args_of(built, "--tag") == ["tool:1.2.3-arm64"]
    assert args_of(built, "--build-context")[0] == f"deps={deps.resolve()}"
    out = repo / "dist" / "tool-1.2.3-image-arm64.tar"
    assert saved == ["podman", "save", "--format", "oci-archive", "-o", str(out), "tool:1.2.3-arm64"]
    assert "tool-1.2.3-image-arm64.tar" in (repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8")
