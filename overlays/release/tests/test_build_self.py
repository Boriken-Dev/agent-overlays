"""The copy mechanism of tools/build.py: configuration, `self check`, `self update`, `help`."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from conftest import REPO_TOOLS, commit_all, init_repo

SOURCE_LINE = re.compile(
    r"^# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/(?P<name>[\w.]+), at commit SOURCE\.$"
)


def test_every_shared_file_names_its_source_on_line_3():
    files = [p for p in REPO_TOOLS.iterdir() if p.is_file()]
    assert files
    for path in files:
        lines = path.read_text(encoding="utf-8").split("\n")
        assert lines[0].startswith("#!"), path.name
        assert lines[1] == "# SPDX-License-Identifier: MIT", path.name
        found = SOURCE_LINE.match(lines[2])
        assert found and found["name"] == path.name, path.name
        assert lines[3] == "# Copyright (c) 2026 Jose A.", path.name
        assert b"\r" not in path.read_bytes(), path.name


# --- configuration ------------------------------------------------------------------------------


def test_an_empty_configuration_is_all_defaults(build):
    cfg = build.parse_config("")
    assert cfg.tools.files == ["build.py"]
    assert cfg.tools.source == "Boriken-Dev/agent-overlays"
    assert cfg.net.retries == 3


def test_an_unknown_key_is_refused_with_its_path(build):
    with pytest.raises(build.Problem, match=r"build.cfg: tools.fils: unknown key"):
        build.parse_config("[tools]\nfils = []\n")
    with pytest.raises(build.Problem, match=r"nothing: unknown key"):
        build.parse_config("[nothing]\nx = 1\n")


def test_a_wrong_type_is_refused_with_its_path(build):
    with pytest.raises(build.Problem, match=r"tools.files: expected strs"):
        build.parse_config('[tools]\nfiles = "build.py"\n')


def test_a_syntax_error_is_a_problem(build):
    with pytest.raises(build.Problem, match="build.cfg"):
        build.parse_config("[tools\n")


def test_help_config_lists_every_key_with_its_default(build):
    text = build.config_help()
    assert "tools.files  (strs, ['build.py'])" in text
    assert "net.timeout  (int, 120)" in text


def test_help_config_covers_the_whole_schema(build):
    text = build.config_help()

    def names(keys, path=""):
        for name, key in keys.items():
            yield f"{path}{name}"
            if key.keys:
                yield from names(key.keys, f"{path}{name}.")

    for name in names(build.SCHEMA):
        assert name in text, name


# --- self -----------------------------------------------------------------------------------------


@pytest.fixture
def overlay(tmp_path, build):
    """A fake upstream: a git repository holding build.py and one more shared file."""
    repo = init_repo(tmp_path / "upstream")
    tools = repo / "overlays" / "release" / "repo" / "tools"
    tools.mkdir(parents=True)
    shutil.copyfile(REPO_TOOLS / "build.py", tools / "build.py")
    (tools / "extra.py").write_bytes(
        b"#!/usr/bin/env python3\n# SPDX-License-Identifier: MIT\n"
        b"# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/extra.py, at commit SOURCE.\n"
        b"# Copyright (c) 2026 Jose A.\nprint('extra')\n"
    )
    commit = commit_all(repo)
    return repo, commit


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    return root


def run(build, *argv):
    return build.main(list(argv))


def config(target: Path, text: str) -> None:
    (target / "tools" / "build.cfg").write_text(text, encoding="utf-8")


def test_update_writes_the_listed_files_with_the_commit_and_check_passes(build, overlay, target, capsys):
    repo, commit = overlay
    config(target, '[tools]\nfiles = ["build.py", "extra.py"]\n')
    assert run(build, "--root", str(target), "self", "update", "HEAD", "--from", str(repo)) == 0
    for name in ("build.py", "extra.py"):
        lines = (target / "tools" / name).read_text(encoding="utf-8").split("\n")
        assert lines[2] == f"# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/{name}, at commit {commit}."
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 0
    assert "ok:" in capsys.readouterr().out


def test_the_first_copy_needs_no_configuration(build, overlay, target):
    repo, commit = overlay
    assert run(build, "--root", str(target), "self", "update", commit, "--from", str(repo)) == 0
    assert (target / "tools" / "build.py").is_file()
    assert not (target / "tools" / "extra.py").exists()


def test_a_copy_edited_by_one_byte_fails_naming_the_file_and_update_repairs_it(build, overlay, target, capsys):
    repo, commit = overlay
    config(target, '[tools]\nfiles = ["build.py", "extra.py"]\n')
    run(build, "--root", str(target), "self", "update", commit, "--from", str(repo))
    path = target / "tools" / "extra.py"
    path.write_bytes(path.read_bytes().replace(b"'extra'", b"'extrb'"))
    capsys.readouterr()
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 1
    err = capsys.readouterr().err
    assert "build.py: tools/extra.py: differs from the overlay" in err
    assert "tools/build.py" not in err
    assert run(build, "--root", str(target), "self", "update", commit, "--from", str(repo)) == 0
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 0


def test_a_listed_file_that_is_missing_fails(build, overlay, target, capsys):
    repo, commit = overlay
    config(target, '[tools]\nfiles = ["build.py", "extra.py"]\n')
    run(build, "--root", str(target), "self", "update", commit, "--from", str(repo))
    (target / "tools" / "extra.py").unlink()
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 1
    assert "tools/extra.py: listed in [tools] files, missing" in capsys.readouterr().err


def test_an_unlisted_file_with_the_header_fails(build, overlay, target, capsys):
    repo, commit = overlay
    config(target, '[tools]\nfiles = ["build.py", "extra.py"]\n')
    run(build, "--root", str(target), "self", "update", commit, "--from", str(repo))
    config(target, '[tools]\nfiles = ["build.py"]\n')
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 1
    assert "tools/extra.py: has the overlay's header but is not in [tools] files" in capsys.readouterr().err


def test_copies_naming_different_commits_fail(build, overlay, target, capsys):
    repo, first = overlay
    tools = repo / "overlays" / "release" / "repo" / "tools"
    config(target, '[tools]\nfiles = ["build.py", "extra.py"]\n')
    run(build, "--root", str(target), "self", "update", first, "--from", str(repo))
    (tools / "extra.py").write_bytes((tools / "extra.py").read_bytes() + b"# more\n")
    second = commit_all(repo)
    run(build, "--root", str(target), "self", "update", second, "--from", str(repo))
    # Put the first commit's extra.py back: its header names an older commit than build.py's.
    data = (target / "tools" / "build.py").read_bytes().replace(second.encode(), first.encode())
    (target / "tools" / "build.py").write_bytes(data)
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 1
    assert "copies name different commits" in capsys.readouterr().err


def test_a_header_still_saying_source_is_refused(build, overlay, target, capsys):
    repo, _ = overlay
    shutil.copyfile(REPO_TOOLS / "build.py", target / "tools" / "build.py")
    assert run(build, "--root", str(target), "self", "check", "--from", str(repo)) == 1
    assert "still says SOURCE" in capsys.readouterr().err


def test_check_downloads_the_overlay_files_when_there_is_no_local_checkout(build, overlay, target, monkeypatch, capsys):
    repo, commit = overlay
    run(build, "--root", str(target), "self", "update", commit, "--from", str(repo))
    seen = []

    def fake(url):
        seen.append(url)
        name = url.rsplit("/", 1)[1]
        return (repo / "overlays" / "release" / "repo" / "tools" / name).read_bytes()

    monkeypatch.setattr(build, "fetch", fake)
    assert run(build, "--root", str(target), "self", "check") == 0
    assert seen == [f"https://raw.githubusercontent.com/Boriken-Dev/agent-overlays/{commit}/overlays/release/repo/tools/build.py"]
    path = target / "tools" / "build.py"
    path.write_bytes(path.read_bytes() + b"# edited\n")
    assert run(build, "--root", str(target), "self", "check") == 1


def test_update_resolves_a_branch_with_ls_remote(build, overlay, target, monkeypatch):
    repo, commit = overlay
    asked = []

    def fake_git(*args, **kwargs):
        if args[0] == "ls-remote":
            asked.append(args[1])
            return f"{commit}\trefs/heads/main"
        return real(*args, **kwargs)

    real = build.run_git
    monkeypatch.setattr(build, "run_git", fake_git)
    monkeypatch.setattr(
        build, "fetch", lambda url: (repo / "overlays" / "release" / "repo" / "tools" / url.rsplit("/", 1)[1]).read_bytes()
    )
    assert run(build, "--root", str(target), "self", "update", "main") == 0
    assert asked == ["https://github.com/Boriken-Dev/agent-overlays.git"]
    assert commit in (target / "tools" / "build.py").read_text(encoding="utf-8").split("\n")[2]


def test_the_overlays_own_copy_needs_a_root(build, capsys):
    with pytest.raises(SystemExit) as stop:
        build.main(["self", "check"])
    assert stop.value.code == 2
    assert "--root" in capsys.readouterr().err


def test_help_prints_every_section(build, capsys):
    assert build.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "self" in out and "build.py self check" in out
    assert build.main(["help", "config"]) == 0
    assert "tools.files" in capsys.readouterr().out
