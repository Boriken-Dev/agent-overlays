"""`build.py sdk`: the lock's lint, its agreement with the repository, and installing it."""

from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

from conftest import commit_all, git, init_repo
from test_build_deps import LOCK as CARGO_LOCK_TEXT, ctx_for, make_tree_commit

RUSTUP = b"#!/bin/sh\necho rustup\n"
RUSTUP_SHA = __import__("hashlib").sha256(RUSTUP).hexdigest()
DEPS = "d" * 40


def lock_text(*rows: str) -> str:
    return "# lock\n" + "\n".join(rows) + "\n"


def good_rows() -> list[str]:
    rows = [f"core rustup-init 1.29.2 {p} {RUSTUP_SHA} https://static.rust-lang.org/rustup/archive/1.29.2/{p}/rustup-init" for p in
            ("windows-x64", "windows-arm64", "linux-x64", "linux-arm64", "macos-arm64", "macos-x64")]
    rows += [
        "core rust 1.99.1 any - -",
        "core rust 1.89.0 any - -",
        "core rust-component clippy any - -",
        "core rust-component rustfmt any - -",
        "core rust-target wasm32-unknown-unknown any - -",
    ]
    return rows


def sdk_repo(build, tmp_path, rows: list[str] | None = None, cfg: str = "") -> Path:
    root = init_repo(tmp_path / "repo")
    ctx_for(build, root, cfg)
    (root / "tools" / "sdk.lock").write_bytes(lock_text(*(rows if rows is not None else good_rows())).encode())
    return root


def check(build, root, *extra) -> int:
    return build.main(["--root", str(root), "sdk", "check", *extra])


# --- lint -----------------------------------------------------------------------------------------------------


def test_a_good_lock_passes(build, tmp_path, capsys):
    root = sdk_repo(build, tmp_path)
    assert check(build, root) == 0
    assert "ok: 11 items in sdk.lock" in capsys.readouterr().out


def lint(build, tmp_path, row_or_text: str, cfg: str = "", replace_all: bool = False):
    rows = [row_or_text] if replace_all else good_rows() + [row_or_text]
    ctx = ctx_for(build, tmp_path / "x", cfg)
    return build.lint_lock(ctx, lock_text(*rows).encode(), "sdk.lock")[1]


@pytest.mark.parametrize(
    "row,message",
    [
        ("core only three", "expected 6 fields, found 3"),
        ("mystery thing 1 any - -", "unknown group mystery"),
        ("core thing 1 plan9-x64 - -", "unknown platform plan9-x64"),
        ("core thing 1 any abc -", "hash of thing is not 64 hex characters: abc"),
        ("core thing 1 any - ftp://x/y", "url of thing is not https or docker: ftp://x/y"),
        ("core thing latest any - -", "thing floats: latest"),
        (f"core thing 1 any {'a' * 64} https://x.test/y", None),
        ("core thing 1 any - https://x.test/y.tar.gz", "download thing has no hash"),
        ("app tool abc123 any - https://x.test/y.git", "git item tool is not at a commit: abc123"),
        (f"system git core any {'a' * 64} -", "system item git has a hash or a url"),
        ("system git nogroup any - -", "system item git names no group: nogroup"),
        ("core rust 1.89 any - -", "rust is not an exact version: 1.89"),
        ("core rust 1.89.0 any - -", "same item as line"),
    ],
)
def test_lint_names_each_malformed_row(build, tmp_path, row, message):
    errors = lint(build, tmp_path, row)
    if message is None:
        assert errors == []
    else:
        assert any(message in e for e in errors), errors


def test_lint_reports_the_line_number(build, tmp_path):
    errors = lint(build, tmp_path, "core only three")
    assert errors[0].startswith("sdk.lock:13: ")


def test_a_floating_name_is_not_a_pin_for_any_of_the_known_spellings(build, tmp_path):
    for name in ("stable", "beta", "nightly", "lts", "main", "master", "head", "dev", "Latest"):
        assert any("floats" in e for e in lint(build, tmp_path, f"core t {name} any - -")), name


def test_a_lock_over_the_size_setting_or_with_cr_endings_is_refused(build, tmp_path):
    ctx = ctx_for(build, tmp_path / "x", "[sdk]\nmax_bytes = 10\n")
    errors = build.lint_lock(ctx, b"core rust 1.89.0 any - -\r\n", "sdk.lock")[1]
    assert any("over the 10" in e for e in errors) and any("CR line endings" in e for e in errors)


def test_a_platform_the_lock_lacks_for_a_required_row_is_named(build, tmp_path):
    rows = [r for r in good_rows() if "macos-x64" not in r]
    errors = lint(build, tmp_path, "core rust 1.50.0 any - -", replace_all=False)
    assert errors == []
    ctx = ctx_for(build, tmp_path / "y")
    errors = build.lint_lock(ctx, lock_text(*rows).encode(), "sdk.lock")[1]
    assert errors == ["sdk.lock: core rustup-init is missing macos-x64"]


def test_groups_platforms_and_required_rows_are_settings(build, tmp_path):
    cfg = '[sdk]\ngroups = ["core", "release"]\nplatforms = ["linux-x64"]\nall_platforms = []\n'
    row = f"core rustup-init 1 linux-x64 {RUSTUP_SHA} https://x.test/r"
    assert lint(build, tmp_path, row, cfg, replace_all=True) == []
    assert lint(build, tmp_path, "release rust-target t any - -", cfg, replace_all=True) == []
    assert any("unknown group app" in e for e in lint(build, tmp_path, "app x 1 any - -", cfg, replace_all=True))
    assert any("unknown platform macos-x64" in e for e in lint(build, tmp_path, "core x 1 macos-x64 - -", cfg, replace_all=True))


def test_the_lock_must_exist(build, tmp_path, capsys):
    root = tmp_path / "bare"
    ctx_for(build, root)
    assert check(build, root) == 1
    assert "tools/sdk.lock: no such file" in capsys.readouterr().err


# --- agree ------------------------------------------------------------------------------------------------------


def agree_repo(build, tmp_path, rule: str, file_text: str, name: str = "facts.txt", rows: list[str] | None = None):
    root = sdk_repo(build, tmp_path, rows, f"[[sdk.agree]]\n{rule}\n")
    (root / name).write_text(file_text, encoding="utf-8")
    return root


def test_equal_compares_every_value_with_the_row(build, tmp_path, capsys):
    rule = 'item = "core rustup-init"\nplatform = "linux-x64"\nexpect = "{sha256}"\nfile = "facts.txt"\npattern = "^ARG SHA=(\\\\S+)$"'
    root = agree_repo(build, tmp_path, rule, f"ARG SHA={RUSTUP_SHA}\n")
    assert check(build, root) == 0
    (root / "facts.txt").write_text("ARG SHA=beef\n", encoding="utf-8")
    assert check(build, root) == 1
    assert f"facts.txt has ['beef'], the lock {RUSTUP_SHA}" in capsys.readouterr().err


def test_equal_with_several_rows_asks_for_platform_first_or_set(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfile = "facts.txt"\npattern = "rust=(\\\\S+)"'
    root = agree_repo(build, tmp_path, rule, "rust=1.99.1\n")
    assert check(build, root) == 1
    assert "`equal` needs one row" in capsys.readouterr().err
    root = agree_repo(build, tmp_path / "b", rule + "\nfirst = true", "rust=1.99.1\n")
    assert check(build, root) == 0


def test_prefix_accepts_a_dotted_prefix_of_any_row(build, tmp_path, capsys):
    rule = 'item = "core rust"\nmatch = "prefix"\nfile = "Cargo.toml"\npattern = "^rust-version = \\"([^\\"]+)\\""'
    root = agree_repo(build, tmp_path, rule, 'rust-version = "1.89"\n', "Cargo.toml")
    assert check(build, root) == 0
    (root / "Cargo.toml").write_text('rust-version = "1.8"\n', encoding="utf-8")
    assert check(build, root) == 1
    assert "has 1.8, which is not in the lock's" in capsys.readouterr().err
    (root / "Cargo.toml").write_text('rust-version = "1.89.0"\n', encoding="utf-8")
    assert check(build, root) == 0


def test_set_requires_exactly_the_rows(build, tmp_path, capsys):
    rule = 'item = "core rust"\nmatch = "set"\nfile = "facts.txt"\npattern = "rust ([0-9.]+)"'
    root = agree_repo(build, tmp_path, rule, "rust 1.99.1\nrust 1.89.0\nrust 1.89.0\n")
    assert check(build, root) == 0
    (root / "facts.txt").write_text("rust 1.99.1\n", encoding="utf-8")
    assert check(build, root) == 1
    assert "names ['1.99.1'], the lock ['1.89.0', '1.99.1']" in capsys.readouterr().err


def test_in_needs_the_row_among_the_values(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nmatch = "in"\nfile = "facts.json"\npattern = "\\"([0-9.]+)\\""'
    root = agree_repo(build, tmp_path, rule, '["1.0.0", "1.99.1"]', "facts.json")
    assert check(build, root) == 0
    (root / "facts.json").write_text('["1.0.0"]', encoding="utf-8")
    assert check(build, root) == 1
    assert "does not name 1.99.1" in capsys.readouterr().err


def test_expect_is_a_template_over_the_row(build, tmp_path):
    rows = good_rows() + [f"test postgres 17 any {'3' * 64} docker://docker.io/library/postgres"]
    rule = 'item = "test postgres"\nmatch = "set"\nexpect = "postgres:{version}@sha256:{sha256}"\nfile = "ci.yml"\npattern = "(postgres:\\\\d\\\\S*)"'
    root = agree_repo(build, tmp_path, rule, f"image: postgres:17@sha256:{'3' * 64}\n", "ci.yml", rows)
    assert check(build, root) == 0
    (root / "ci.yml").write_text(f"image: postgres:17@sha256:{'4' * 64}\n", encoding="utf-8")
    assert check(build, root) == 1


def test_a_pattern_with_no_match_a_missing_file_and_a_missing_row_are_problems(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nfile = "facts.txt"\npattern = "nothing here"'
    root = agree_repo(build, tmp_path, rule, "x\n")
    assert check(build, root) == 1
    assert "facts.txt has no match for nothing here" in capsys.readouterr().err
    (root / "facts.txt").unlink()
    assert check(build, root) == 1
    assert "facts.txt: no such file" in capsys.readouterr().err
    root = agree_repo(build, tmp_path / "c", rule.replace("core rust", "core nothing"), "x\n")
    assert check(build, root) == 1
    assert "agree core nothing: the lock has no such row" in capsys.readouterr().err


def test_a_unique_match_mode_is_refused_with_the_rule(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nmatch = "roughly"\nfile = "facts.txt"\npattern = "(x)"'
    root = agree_repo(build, tmp_path, rule, "x\n")
    assert check(build, root) == 1
    assert "unknown match 'roughly'" in capsys.readouterr().err


def test_a_rule_reads_a_git_ref_and_a_file_missing_there_is_a_note(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nref = "HEAD"\nfile = "facts.txt"\npattern = "rust=(\\\\S+)"'
    root = agree_repo(build, tmp_path, rule, "rust=1.99.1\n")
    commit_all(root)
    (root / "facts.txt").write_text("rust=0.0.0\n", encoding="utf-8")
    assert check(build, root) == 0, "the commit holds the agreeing text, the checkout does not"
    root2 = agree_repo(build, tmp_path / "d", rule.replace("facts.txt", "nothere.txt"), "x\n")
    commit_all(root2)
    capsys.readouterr()
    assert check(build, root2) == 0
    assert "note: no nothere.txt at HEAD: not checked" in capsys.readouterr().out


def test_deps_ref_names_where_deps_files_are_read(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nref = "@deps"\nfile = "Dockerfile"\npattern = "^ARG RUST=(\\\\S+)$"'
    root = agree_repo(build, tmp_path, rule, "x\n")
    git(root, "checkout", "-q", "-b", "side")
    (root / "Dockerfile").write_text("ARG RUST=1.99.1\n", encoding="utf-8")
    commit_all(root)
    assert check(build, root, "--deps-ref", "side") == 0
    (root / "Dockerfile").write_text("ARG RUST=1.0.0\n", encoding="utf-8")
    commit_all(root, "again")
    assert check(build, root, "--deps-ref", "side") == 1
    assert "Dockerfile at side has ['1.0.0'], the lock 1.99.1" in capsys.readouterr().err


# --- the deps pin -------------------------------------------------------------------------------------------------


def pinned_repo(build, tmp_path):
    """A repository whose lock pins a commit holding what its Cargo.lock names."""
    rows = good_rows() + [f"core deps {DEPS} any - https://example.test/org/repo.git"]
    root = sdk_repo(build, tmp_path, rows, '[deps]\npin = { lock = "tools/sdk.lock", item = "core deps" }\n')
    checksum = "a" * 64
    (root / "Cargo.lock").write_text(CARGO_LOCK_TEXT.split('[[package]]\nname = "lib"')[0], encoding="utf-8")
    sums = json.dumps({"files": {"Cargo.toml": build.sha256(b"[package]\n")}, "package": checksum}).encode()
    commit = make_tree_commit(
        root,
        {
            "crates/dep-1.2.3/Cargo.toml": ("100644", b"[package]\n"),
            f"crates/dep-1.2.3/{build.CHECKSUM}": ("100644", sums),
            "manifest.json": ("100644", json.dumps({"format": 1, "crates": 1, "gitSources": 0}).encode()),
        },
    )
    path = root / "tools" / "sdk.lock"
    path.write_bytes(path.read_bytes().decode().replace(DEPS, commit).encode())
    return root, commit


def test_the_deps_pin_is_held_to_cargo_lock(build, tmp_path, capsys):
    root, commit = pinned_repo(build, tmp_path)
    assert check(build, root) == 0
    (root / "Cargo.lock").write_text(
        (root / "Cargo.lock").read_text().replace("a" * 64, "b" * 64), encoding="utf-8"
    )
    assert check(build, root) == 1
    err = capsys.readouterr().err
    assert f"tools/sdk.lock:13: deps at {commit[:12]}: crates/dep-1.2.3: package checksum" in err


def test_a_pin_that_is_not_here_says_how_to_fetch_it(build, tmp_path, capsys):
    root, commit = pinned_repo(build, tmp_path)
    path = root / "tools" / "sdk.lock"
    path.write_bytes(path.read_bytes().decode().replace(commit, "e" * 40).encode())
    assert check(build, root) == 1
    assert "is not here: git fetch --depth 1 origin " + "e" * 40 in capsys.readouterr().err


def test_a_pin_that_is_not_a_commit_is_a_problem(build, tmp_path, capsys):
    root, commit = pinned_repo(build, tmp_path)
    path = root / "tools" / "sdk.lock"
    path.write_bytes(path.read_bytes().decode().replace(commit, "v1.0.0").encode())
    assert check(build, root) == 1
    err = capsys.readouterr().err
    assert "git item deps is not at a commit: v1.0.0" in err and "the deps pin is not a commit: v1.0.0" in err


def test_at_pin_reads_a_file_of_the_pinned_commit(build, tmp_path, capsys):
    root, commit = pinned_repo(build, tmp_path)
    rule = '\n[[sdk.agree]]\nitem = "core deps"\nref = "@pin"\nfile = "manifest.json"\npattern = "\\"format\\": (\\\\d+)"\nexpect = "1"\n'
    (root / "tools" / "build.cfg").write_text(
        (root / "tools" / "build.cfg").read_text() + rule.replace('expect = "1"', 'expect = "1"'), encoding="utf-8"
    )
    assert check(build, root) == 0
    (root / "tools" / "build.cfg").write_text(
        (root / "tools" / "build.cfg").read_text().replace('expect = "1"', 'expect = "2"'), encoding="utf-8"
    )
    assert check(build, root) == 1
    assert "manifest.json at " + commit[:12] in capsys.readouterr().err


def test_at_pin_without_a_pin_is_a_problem(build, tmp_path, capsys):
    rule = 'item = "core rust"\nfirst = true\nref = "@pin"\nfile = "x"\npattern = "(x)"'
    root = agree_repo(build, tmp_path, rule, "x")
    assert check(build, root) == 1
    assert "ref @pin needs [deps] pin" in capsys.readouterr().err


# --- online -----------------------------------------------------------------------------------------------------------


def test_online_compares_each_hash_with_its_publisher(build, tmp_path, monkeypatch, capsys):
    node = "9" * 64
    rows = good_rows() + [
        f"test node 22.1.0 linux-x64 {node} https://nodejs.org/dist/v22.1.0/node-v22.1.0-linux-x64.tar.xz",
        f"core llvm 22.1.8 windows-arm64 {'8' * 64} https://github.com/llvm/llvm-project/releases/download/llvmorg-22.1.8/clang.tar.xz",
        f"core other 1 any {'7' * 64} https://elsewhere.test/other.tar.gz",
    ]
    root = sdk_repo(build, tmp_path, rows)
    asked = []

    def fake(url):
        asked.append(url)
        if url.endswith("/rustup-init.sha256") or url.endswith(".sha256"):
            return f"{RUSTUP_SHA}  rustup-init\n".encode()
        if url.endswith("SHASUMS256.txt"):
            return f"{node}  node-v22.1.0-linux-x64.tar.xz\n".encode()
        if "api.github.com" in url:
            return json.dumps({"assets": [{"name": "clang.tar.xz", "digest": "sha256:" + "8" * 64}]}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(build, "fetch", fake)
    assert check(build, root, "--online") == 0
    assert not any("elsewhere.test" in u for u in asked), "an unknown host has no publisher to ask"
    assert any(u.endswith("/SHASUMS256.txt") for u in asked) and any("repos/llvm/llvm-project/releases/tags/llvmorg-22.1.8" in u for u in asked)
    monkeypatch.setattr(build, "fetch", lambda url: (b"f" * 64 + b"  x\n") if url.endswith(".sha256") else fake(url))
    assert check(build, root, "--online") == 1
    assert "rustup-init linux-x64: upstream says " + "f" * 64 in capsys.readouterr().err


# --- install -----------------------------------------------------------------------------------------------------------


class Machine:
    """A fake rustup and its state, standing in for the tools `sdk install` runs."""

    def __init__(self, build, monkeypatch):
        self.build = build
        self.commands: list[list[str]] = []
        self.toolchains: dict[str, tuple[list[str], list[str]]] = {}
        self.default = ""
        self.installed_version = None
        monkeypatch.setattr(build, "run_tool", self.run)
        monkeypatch.setattr(build, "capture_tool", self.capture)

    def run(self, command, env=None, cwd=None):
        command = [str(c) for c in command]
        self.commands.append(command)
        if Path(command[0]).name == "rustup-init":
            home = Path(env["CARGO_HOME"]) / "bin"
            home.mkdir(parents=True)
            (home / "rustup").write_bytes(b"x")
            self.installed_version = "1.29.2"
        elif command[1:3] == ["toolchain", "install"]:
            comps = [command[i + 1] for i, c in enumerate(command) if c == "-c"]
            targets = [command[i + 1] for i, c in enumerate(command) if c == "-t"]
            self.toolchains[command[3]] = (comps, targets)
        elif command[1] == "default":
            self.default = command[2]

    def capture(self, command, env=None):
        command = [str(c) for c in command]
        name = Path(command[0]).name
        if name == "rustup" and command[1:] == ["--version"]:
            return (0, f"rustup {self.installed_version} (abc)\n") if self.installed_version else (127, "")
        if name == "rustup" and command[1:] == ["toolchain", "list"]:
            return 0, "".join(f"{v}-x86_64-unknown-linux-gnu\n" for v in self.toolchains)
        if name == "rustup" and command[1] == "component":
            comps = self.toolchains.get(command[4], ([], []))[0]
            return 0, "".join(f"{c}-x86_64-unknown-linux-gnu\n" for c in comps)
        if name == "rustup" and command[1] == "target":
            return 0, "".join(f"{t}\n" for t in self.toolchains.get(command[4], ([], []))[1])
        if name == "rustup" and command[1:] == ["default"]:
            return 0, f"{self.default} (default)\n" if self.default else ""
        return 127, ""


@pytest.fixture
def machine(build, monkeypatch):
    monkeypatch.setattr(build, "fetch", lambda url: RUSTUP)
    monkeypatch.delenv("BUILD_SDK_HOME", raising=False)
    monkeypatch.delenv("BUILD_SDK_PLATFORM", raising=False)
    return Machine(build, monkeypatch)


def install(build, root, prefix, *extra) -> int:
    return build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--prefix", str(prefix), *extra])


def test_install_fetches_rustup_verified_then_installs_each_toolchain_with_its_parts(build, tmp_path, machine, capsys):
    root = sdk_repo(build, tmp_path)
    prefix = tmp_path / "sdk"
    assert install(build, root, prefix) == 0
    kept = prefix / "dl" / "rustup-init-1.29.2" / "rustup-init"
    assert kept.read_bytes() == RUSTUP
    cmds = machine.commands
    assert cmds[0][1:] == ["-y", "--no-modify-path", "--default-toolchain", "none", "--profile", "minimal"]
    assert cmds[1][1:] == ["set", "auto-self-update", "disable"]
    assert cmds[2][1:] == ["toolchain", "install", "1.99.1", "--profile", "minimal", "--no-self-update", "-c", "clippy", "-c", "rustfmt", "-t", "wasm32-unknown-unknown"]
    assert cmds[3][3] == "1.89.0"
    assert cmds[4][1:] == ["default", "1.99.1-x86_64-unknown-linux-gnu"]
    out = capsys.readouterr().out
    assert "rustup 1.29.2" in out and "installed" in out
    env_sh = (prefix / "env.sh").read_text()
    assert f"export RUSTUP_HOME='{prefix / 'rust' / 'rustup'}'" in env_sh
    assert f"export CARGO_HOME='{prefix / 'rust' / 'cargo'}'" in env_sh
    assert f"export BUILD_SDK_HOME='{prefix}'" in env_sh
    assert str(prefix / "rust" / "cargo" / "bin") in env_sh
    assert "$env:RUSTUP_HOME = " in (prefix / "env.ps1").read_text()
    assert not list(prefix.glob("tmp.*")), "the download directory is removed"


def test_a_second_install_does_nothing_and_stays_off_the_network(build, tmp_path, machine, monkeypatch, capsys):
    root = sdk_repo(build, tmp_path)
    prefix = tmp_path / "sdk"
    install(build, root, prefix)
    machine.commands.clear()
    monkeypatch.setattr(build, "fetch", lambda url: (_ for _ in ()).throw(AssertionError("network used")))
    capsys.readouterr()
    assert install(build, root, prefix) == 0
    assert machine.commands == []
    assert capsys.readouterr().out.count("ok") >= 3


def test_a_download_with_the_wrong_hash_is_refused_and_kept_nowhere(build, tmp_path, machine, monkeypatch, capsys):
    root = sdk_repo(build, tmp_path)
    monkeypatch.setattr(build, "fetch", lambda url: b"tampered")
    assert install(build, root, tmp_path / "sdk") == 1
    err = capsys.readouterr().err
    assert "refused https://static.rust-lang.org" in err and "the lock says " + RUSTUP_SHA in err
    assert not (tmp_path / "sdk" / "dl" / "rustup-init-1.29.2").exists()
    assert machine.commands == []
    assert not list((tmp_path / "sdk").glob("tmp.*"))


def test_a_kept_download_that_changed_on_disk_is_refused_and_removed(build, tmp_path, machine, capsys):
    root = sdk_repo(build, tmp_path)
    prefix = tmp_path / "sdk"
    kept = prefix / "dl" / "rustup-init-1.29.2" / "rustup-init"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"changed")
    assert install(build, root, prefix) == 1
    assert "refused and removed" in capsys.readouterr().err and not kept.exists()


def test_check_installs_nothing_and_says_what_is_missing(build, tmp_path, machine, capsys):
    root = sdk_repo(build, tmp_path)
    assert install(build, root, tmp_path / "sdk", "--check") == 1
    out = capsys.readouterr().out
    assert "rustup 1.29.2" in out and "missing, would fetch https://static.rust-lang.org" in out
    assert "rust 1.99.1" in out and "missing, rustup would fetch it with clippy rustfmt wasm32-unknown-unknown" in out
    assert machine.commands == [] and not (tmp_path / "sdk").exists()
    install(build, root, tmp_path / "sdk")
    assert install(build, root, tmp_path / "sdk", "--check") == 0


def test_the_groups_setting_and_flag_choose_what_is_installed(build, tmp_path, machine, capsys):
    rows = good_rows() + ["release rust-target x86_64-unknown-linux-musl any - -"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ngroups = ["core", "release"]\ninstall = ["core", "release"]\n')
    install(build, root, tmp_path / "a")
    assert "-t" in machine.commands[2] and "x86_64-unknown-linux-musl" in machine.commands[2]
    machine.commands.clear()
    machine.toolchains.clear()
    install(build, root, tmp_path / "b", "--groups", "core")
    assert "x86_64-unknown-linux-musl" not in machine.commands[2]
    with pytest.raises(SystemExit) as stop:
        install(build, root, tmp_path / "c", "--groups", "nonsense")
    assert stop.value.code == 2


def test_a_nightly_toolchain_gets_no_components_and_a_cargo_tool_is_installed_with_the_stable(build, tmp_path, machine, capsys):
    rows = good_rows() + ["api rust nightly-2026-09-21 any - -", "api cargo-public-api 0.52.0 any - -"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "api"]\n')
    assert install(build, root, tmp_path / "sdk") == 0
    nightly = next(c for c in machine.commands if "nightly-2026-09-21" in c)
    assert "-c" not in nightly and "-t" not in nightly
    tool = next(c for c in machine.commands if "cargo" in c and "install" in c)
    assert tool[1:] == ["run", "1.99.1", "cargo", "install", "cargo-public-api", "--version", "0.52.0", "--locked"]


def make_tar(prefix: str, files: dict[str, bytes], link: tuple[str, str] | None = None) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        if link:
            info = tarfile.TarInfo(f"{prefix}/{link[0]}")
            info.type = tarfile.SYMTYPE
            info.linkname = link[1]
            tar.addfile(info)
    return out.getvalue()


def test_an_archive_item_is_verified_unpacked_without_its_top_directory_and_put_on_path(build, tmp_path, machine, monkeypatch):
    body = make_tar("node-v1", {"bin/node": b"binary", "lib/x.js": b"x"})
    rows = good_rows() + [f"test node 1.0.0 linux-x64 {build.sha256(body)} https://nodejs.org/dist/v1.0.0/node-v1.0.0-linux-x64.tar.gz"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "test"]\n')
    monkeypatch.setattr(build, "fetch", lambda url: RUSTUP if "rustup" in url else body)
    prefix = tmp_path / "sdk"
    assert install(build, root, prefix) == 0
    assert (prefix / "node" / "1.0.0" / "bin" / "node").read_bytes() == b"binary"
    assert not (prefix / "node" / "1.0.0" / "node-v1").exists()
    assert str(prefix / "node" / "1.0.0" / "bin") in (prefix / "env.sh").read_text()
    machine.commands.clear()
    assert install(build, root, prefix) == 0
    assert machine.commands == []


def test_a_zip_item_without_a_bin_directory_is_itself_on_path(build, tmp_path, machine, monkeypatch):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("node-v1/node.exe", b"exe")
    body = out.getvalue()
    rows = good_rows() + [f"test node 1.0.0 linux-x64 {build.sha256(body)} https://nodejs.org/dist/v1.0.0/node-v1.0.0-win-x64.zip"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "test"]\n')
    monkeypatch.setattr(build, "fetch", lambda url: RUSTUP if "rustup" in url else body)
    prefix = tmp_path / "sdk"
    assert install(build, root, prefix) == 0
    assert (prefix / "node" / "1.0.0" / "node.exe").read_bytes() == b"exe"
    assert str(prefix / "node" / "1.0.0") in (prefix / "env.sh").read_text()


def test_an_archive_with_a_path_out_of_it_or_a_zip_link_is_refused(build, tmp_path, machine, monkeypatch, capsys):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("../escape", b"x")
    body = out.getvalue()
    rows = good_rows() + [f"test node 1.0.0 linux-x64 {build.sha256(body)} https://nodejs.org/dist/v1.0.0/node.zip"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "test"]\n')
    monkeypatch.setattr(build, "fetch", lambda url: RUSTUP if "rustup" in url else body)
    assert install(build, root, tmp_path / "sdk") == 1
    assert "outside the archive" in capsys.readouterr().err
    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as z:
        info = zipfile.ZipInfo("top/link")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(build.Problem, match="links are refused"):
        build.safe_unzip(zipfile.ZipFile(io.BytesIO(link.getvalue())), tmp_path / "z")


def test_unpacking_an_unknown_archive_kind_is_refused(build, tmp_path):
    with pytest.raises(build.Problem, match="only .zip"):
        build.unpack(tmp_path / "x.rar", tmp_path / "out")


def test_a_tar_symlink_inside_the_archive_is_made_on_posix_and_one_outside_is_refused(build, tmp_path):
    if build.os.name == "nt":
        build.safe_extract(tarfile.open(fileobj=io.BytesIO(make_tar("top", {"a": b"x"}, ("l", "a"))), mode="r:gz"), tmp_path / "w", links="inside", strip=1)
        assert (tmp_path / "w" / "a").is_file()
        return
    build.safe_extract(tarfile.open(fileobj=io.BytesIO(make_tar("top", {"a": b"x"}, ("l", "a"))), mode="r:gz"), tmp_path / "w", links="inside", strip=1)
    assert (tmp_path / "w" / "l").is_symlink()
    with pytest.raises(build.Problem, match="points outside"):
        build.safe_extract(tarfile.open(fileobj=io.BytesIO(make_tar("top", {"a": b"x"}, ("l", "../../etc"))), mode="r:gz"), tmp_path / "v", links="inside", strip=1)


def test_a_git_item_is_cloned_at_its_commit_runs_its_post_command_once_and_gets_its_env(build, tmp_path, machine, monkeypatch):
    origin = init_repo(tmp_path / "origin")
    (origin / "bin").mkdir()
    (origin / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    commit = commit_all(origin)
    (origin / "later.txt").write_text("x", encoding="utf-8")
    commit_all(origin, "later")
    rows = good_rows() + [f"app tool {commit} any - https://example.test/org/tool.git"]
    cfg = (
        f'[sdk]\ninstall = ["core", "app"]\ngroups = ["core", "app"]\ngit_urls = {{ tool = "{origin.as_uri()}" }}\n'
        'path = ["{dir:app tool}/bin"]\n'
        '[sdk.env]\nTOOL_ROOT = "{dir:app tool}"\n'
        '[[sdk.post]]\nitem = "app tool"\nrun = ["bin/tool", "precache"]\n'
    )
    root = sdk_repo(build, tmp_path, rows, cfg)
    prefix = tmp_path / "sdk"
    assert install(build, root, prefix) == 0
    clone = prefix / "git" / "tool" / commit[:12]
    assert git(clone, "rev-parse", "HEAD") == commit
    assert not (clone / "later.txt").exists(), "the clone is at the locked commit, not the branch tip"
    post = [c for c in machine.commands if c[0].endswith("tool")]
    assert len(post) == 1 and post[0][1:] == ["precache"]
    assert f"export TOOL_ROOT='{clone}'" in (prefix / "env.sh").read_text()
    machine.commands.clear()
    assert install(build, root, prefix) == 0
    assert [c for c in machine.commands if c[0].endswith("tool")] == [], "the stamp records that it ran"


def test_the_git_url_comes_from_the_environment_then_the_setting_then_the_lock(build, tmp_path, machine, monkeypatch, capsys):
    origin = init_repo(tmp_path / "origin")
    (origin / "f").write_text("x", encoding="utf-8")
    commit = commit_all(origin)
    rows = good_rows() + [f"app my-tool {commit} any - https://example.test/org/tool.git"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "app"]\ngroups = ["core", "app"]\ngit_urls = { my-tool = "https://wrong.test/x.git" }\n')
    monkeypatch.setenv("BUILD_SDK_GIT_URL_MY_TOOL", origin.as_uri())
    assert install(build, root, tmp_path / "sdk") == 0
    assert (tmp_path / "sdk" / "git" / "my-tool" / commit[:12] / "f").is_file()


def test_a_venv_is_made_from_the_hashed_requirements_and_reused_while_they_are_unchanged(build, tmp_path, machine, monkeypatch):
    rows = good_rows() + ["docs mkdocs 1.6.1 any - -"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "docs"]\n[[sdk.venv]]\nitem = "docs mkdocs"\nrequirements = "tools/docs.txt"\n')
    (root / "tools" / "docs.txt").write_text("mkdocs==1.6.1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    prefix = tmp_path / "sdk"
    venv = prefix / "venv" / "mkdocs-1.6.1"
    original = machine.run

    def run(command, env=None, cwd=None):
        original(command, env, cwd)
        if command[1:3] == ["-m", "venv"]:
            (venv / "bin").mkdir(parents=True)
            (venv / "Scripts").mkdir(parents=True)

    monkeypatch.setattr(build, "run_tool", run)
    assert install(build, root, prefix) == 0
    pip = [c for c in machine.commands if "pip" in c]
    assert pip and "--require-hashes" in pip[0] and pip[0][-1].endswith("docs.txt")
    assert (venv / "requirements.txt").read_bytes() == (root / "tools" / "docs.txt").read_bytes()
    machine.commands.clear()
    assert install(build, root, prefix) == 0
    assert not [c for c in machine.commands if "pip" in c]
    (root / "tools" / "docs.txt").write_text("changed\n", encoding="utf-8")
    assert install(build, root, prefix, "--check") == 1


def test_a_docker_row_is_reported_and_left_to_the_container_engine(build, tmp_path, machine, capsys):
    rows = good_rows() + [f"test postgres 17 any {'3' * 64} docker://docker.io/library/postgres"]
    root = sdk_repo(build, tmp_path, rows, '[sdk]\ninstall = ["core", "test"]\n')
    assert install(build, root, tmp_path / "sdk") == 0
    assert f"docker.io/library/postgres:17@sha256:{'3' * 64}, left to your container engine" in capsys.readouterr().out


def test_system_items_are_checked_never_installed(build, tmp_path, machine, capsys):
    rows = good_rows() + ["system git core any - -", "system definitely-not-a-tool-xyz core any - -"]
    cfg = '[sdk.system.print]\nlinux = "sudo apt-get install -y definitely-not-a-tool-xyz"\n'
    root = sdk_repo(build, tmp_path, rows, cfg)
    assert install(build, root, tmp_path / "sdk", "--check") == 1
    captured = capsys.readouterr()
    assert "system git" in captured.out and "found" in captured.out
    assert "system definitely-not-a-tool-xyz" in captured.out and "MISSING; see --print-system" in captured.out
    assert "some of the system group is missing" in captured.err and "sudo apt-get install -y definitely-not-a-tool-xyz" in captured.err
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--print-system"]) == 0
    assert capsys.readouterr().out.strip() == "sudo apt-get install -y definitely-not-a-tool-xyz"


def test_print_system_without_instructions_says_so(build, tmp_path, capsys):
    root = sdk_repo(build, tmp_path)
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "windows-x64", "--print-system"]) == 0
    assert "no system package instructions are configured for windows" in capsys.readouterr().out


def test_the_platform_comes_from_the_flag_the_environment_or_the_machine(build, monkeypatch):
    monkeypatch.setenv("BUILD_SDK_PLATFORM", "macos-arm64")
    assert build.host_platform() == "macos-arm64"
    monkeypatch.delenv("BUILD_SDK_PLATFORM")
    assert re.match(r"^(windows|linux|macos)-(x64|arm64)$", build.host_platform())


def test_windows_arm64_uses_the_locked_llvm_only_where_no_clang_is_found(build, tmp_path, machine, monkeypatch, capsys):
    body = make_tar("clang+llvm-22", {"bin/clang.exe": b"exe"})
    rows = good_rows() + [f"core llvm 22.1.8 windows-arm64 {build.sha256(body)} https://github.com/llvm/llvm-project/releases/download/llvmorg-22.1.8/clang.tar.gz"]
    root = sdk_repo(build, tmp_path, rows)
    monkeypatch.setattr(build, "fetch", lambda url: RUSTUP if "rustup" in url else body)
    monkeypatch.setattr(build.shutil, "which", lambda name: "C:/clang.exe" if name == "clang" else None)
    prefix = tmp_path / "sdk"
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "windows-arm64", "--prefix", str(prefix), "--check"]) == 1
    assert "llvm" not in capsys.readouterr().out, "a clang on PATH means no LLVM is wanted"
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    capsys.readouterr()
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "windows-arm64", "--prefix", str(prefix), "--check"]) == 1
    assert "llvm 22.1.8" in capsys.readouterr().out
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "windows-arm64", "--prefix", str(prefix)]) == 0
    assert (prefix / "llvm" / "22.1.8" / "bin" / "clang.exe").is_file()
    assert str(prefix / "llvm" / "22.1.8" / "bin") in (prefix / "env.ps1").read_text()


def test_a_lock_that_does_not_lint_stops_install(build, tmp_path, machine, capsys):
    root = sdk_repo(build, tmp_path, ["core thing latest any - -"])
    assert install(build, root, tmp_path / "sdk") == 1
    assert "thing floats: latest" in capsys.readouterr().err


def test_the_default_prefix_is_named_for_the_repository_and_the_environment_wins(build, tmp_path, machine, monkeypatch, capsys):
    root = sdk_repo(build, tmp_path)
    monkeypatch.setenv("BUILD_SDK_HOME", str(tmp_path / "from-env"))
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--check"]) == 1
    assert f"prefix                           {tmp_path / 'from-env'}" in capsys.readouterr().out
    monkeypatch.delenv("BUILD_SDK_HOME")
    monkeypatch.setattr(build.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--check"]) == 1
    assert str(tmp_path / "home" / ".repo-sdk") in capsys.readouterr().out


# --- a vendored tool installed offline, and the rust-target rows --------------------------------------------------------

TOOL_CFG = '[sdk]\ninstall = ["core"]\n[[deps.vendor]]\nfolder = "crates-tool"\nitem = "core tool"\nbin = "tool-bin"\n'


def tool_rows() -> list[str]:
    return good_rows() + ["core tool 1.0.0 any " + "a" * 64 + " -"]


def test_check_names_a_vendored_tool_that_needs_a_deps_checkout_and_a_missing_target(build, tmp_path, machine, capsys):
    root = sdk_repo(build, tmp_path, tool_rows(), TOOL_CFG)
    assert install(build, root, tmp_path / "sdk", "--check") == 1
    out = capsys.readouterr().out
    assert "tool 1.0.0" in out and "missing, needs a deps checkout" in out
    assert "rust-target wasm32-unknown-unknown (1.99.1)" in out and "missing" in out
    assert machine.commands == []
    # The first install has no deps checkout to build the tool from, so it says so instead of reaching a registry.
    assert install(build, root, tmp_path / "sdk") == 1
    err = capsys.readouterr().err
    assert "tool 1.0.0 is built offline from crates-tool/ of the deps branch" in err and "pass --deps DIR" in err


def test_a_vendored_tool_is_built_offline_from_its_folder_and_the_target_is_reported(build, tmp_path, machine, capsys):
    from test_build_deps import vendored

    root = sdk_repo(build, tmp_path, tool_rows(), TOOL_CFG)
    lock = (
        'version = 4\n[[package]]\nname = "tool"\nversion = "1.0.0"\n[[package]]\nname = "dep"\nversion = "1.2.3"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "' + "b" * 64 + '"\n'
    )
    deps = tmp_path / "deps"
    vendored(build, deps / "crates-tool", "tool-1.0.0", "a" * 64, extra={"Cargo.lock": lock.encode()})
    vendored(build, deps / "crates-tool", "dep-1.2.3", "b" * 64)
    assert install(build, root, tmp_path / "sdk", "--deps", str(deps)) == 0
    out = capsys.readouterr().out
    assert "rust-target wasm32-unknown-unknown (1.99.1)" in out and "tool 1.0.0" in out and "installed" in out
    built = next(c for c in machine.commands if "install" in c and "--offline" in c)
    assert built[1:4] == ["run", "1.99.1", "cargo"] and built[4] == "--config"
    assert built[6:] == ["install", "--offline", "--locked", "tool", "--version", "1.0.0"]
    config = Path(built[5]).read_text()
    assert (deps / "crates-tool").resolve().as_posix() in config and "offline = true" in config
    assert str(tmp_path / "sdk") in built[5], "the cargo home is written in the prefix, outside the repository"
