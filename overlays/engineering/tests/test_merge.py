"""`merge`: every resolver keeps what both sides wrote and never leaves a marker; `continue` resolves a
rebase or cherry-pick by rule."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools_env import no_machine_agent_files  # noqa: F401  (an autouse fixture)

import _merge

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "skills" / "merge" / "merge.py"
_spec = importlib.util.spec_from_file_location("merge_cmd", MODULE)
merge_cmd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = merge_cmd
_spec.loader.exec_module(merge_cmd)

PY = sys.executable
IDENT = ["-c", "user.name=T", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def conflict(ours, theirs, before="top", after="bottom"):
    return f"{before}\n<<<<<<< HEAD\n{ours}\n=======\n{theirs}\n>>>>>>> branch\n{after}\n"


def put(tmp_path, text, name="f.md", raw=None):
    path = tmp_path / name
    path.write_bytes(raw if raw is not None else text.encode("utf-8"))
    return path


def test_union_keeps_head_then_the_incoming_lines_it_lacks(tmp_path):
    path = put(tmp_path, conflict("- a\n- shared", "- shared\n- b"))
    assert _merge.union(path) == 1
    assert path.read_text(encoding="utf-8") == "top\n- a\n- shared\n- b\nbottom\n"


def test_union_never_drops_structure_lines(tmp_path):
    path = put(tmp_path, conflict("    }\n\n    fn a() {}", "    }\n\n    fn b() {}"))
    _merge.union(path)
    assert path.read_text(encoding="utf-8").count("    }") == 2
    assert "fn b() {}" in path.read_text(encoding="utf-8")


def test_union_reads_latin1_bytes_as_such_and_writes_utf8(tmp_path):
    path = put(tmp_path, "", raw=b"<<<<<<< HEAD\ncaf\xe9\n=======\nna\xefve\n>>>>>>> x\n")
    _merge.union(path)
    assert path.read_bytes() == "café\nnaïve\n".encode("utf-8")


def test_union_handles_crlf_markers(tmp_path):
    path = put(tmp_path, "", raw=b"<<<<<<< HEAD\r\na\r\n=======\r\nb\r\n>>>>>>> x\r\n")
    _merge.union(path)
    assert path.read_bytes() == b"a\r\nb\r\n"


def test_union_handles_several_hunks_and_a_file_with_none(tmp_path):
    path = put(tmp_path, conflict("a", "b") + conflict("c", "d"))
    assert _merge.union(path) == 2
    assert put(tmp_path, "plain\n", "p.md").read_text() == "plain\n"
    assert _merge.union(tmp_path / "p.md") == 0


def test_an_unclosed_marker_is_refused_and_the_file_untouched(tmp_path):
    path = put(tmp_path, "<<<<<<< HEAD\na\n")
    with pytest.raises(_merge.MergeError, match="not closed"):
        _merge.union(path)
    assert path.read_text() == "<<<<<<< HEAD\na\n"


def test_a_marker_inside_the_text_is_not_left_behind(tmp_path):
    path = put(tmp_path, conflict("a\n=======\nx", "b"))
    with pytest.raises(_merge.MergeError):
        _merge.union(path)


def test_rows_keep_heads_and_add_the_new_ones_by_first_cell(tmp_path):
    ours = "| `read` | yes | head |\n| `write` | no | head |"
    theirs = "| `read` | maybe | theirs |\n| `exec` | yes | theirs |"
    path = put(tmp_path, conflict(ours, theirs))
    _merge.rows(path)
    text = path.read_text(encoding="utf-8")
    assert "| `read` | yes | head |" in text and "maybe" not in text and "| `exec` | yes | theirs |" in text


def test_both_blocks_close_the_first_and_keep_the_second(tmp_path):
    path = put(tmp_path, conflict("    #[test]\n    fn a() {\n        1;\n    }", "    #[test]\n    fn b() {\n        2;\n    }", "mod t {", "}"))
    _merge.both_blocks(path)
    text = path.read_text(encoding="utf-8")
    assert text.index("fn a()") < text.index("fn b()") and "<<<<<<<" not in text


def test_show_prints_the_word_differences(tmp_path):
    path = put(tmp_path, conflict("one two three", "one 2 three"))
    report = _merge.show(path)
    assert report[0].startswith("--- conflict 1 at line 2")
    assert "OURS: two || THEIRS: 2" in report[1]


def test_dupes_find_both_versions_of_a_paragraph(tmp_path):
    long = "This paragraph is long enough to be compared by its first sixty characters and then differ"
    path = put(tmp_path, f"{long} in one way.\n{long} in another way.\nunrelated\n")
    assert len(_merge.dupes(path)) == 1
    assert _merge.dupes(put(tmp_path, "short\nshort\n", "s.md")) == []


def test_fix_utf8_turns_stray_bytes_into_the_characters_they_were(tmp_path):
    path = put(tmp_path, "", raw=b"caf\xe9 ok \xe2\x82\xac\n")
    assert _merge.fix_utf8(path) == [0xE9]
    assert path.read_bytes() == "café ok €\n".encode("utf-8")
    assert _merge.fix_utf8(path) == []


PUB = "sdks:\n  dart: x\npackages:\n  alpha:\n    version: \"1\"\n    source: hosted\n  beta:\n    version: \"2\"\n    source: hosted\nsdks2: y\n"


def test_pub_lock_has_every_package_and_the_first_files_entry(tmp_path):
    ours = put(tmp_path, PUB, "o.lock")
    theirs = put(tmp_path, PUB.replace('"2"', '"9"').replace("  beta:", "  gamma:"), "t.lock")
    out = tmp_path / "out.lock"
    assert _merge.pub_lock(ours, theirs, out) == 3
    text = out.read_text(encoding="utf-8")
    assert text.index("alpha") < text.index("beta") < text.index("gamma")
    assert text.count("source: hosted") == 3 and 'version: "2"' in text and 'version: "9"' in text


# --- continue ---------------------------------------------------------------------------------------------------


def git(cwd, *args, check=True):
    done = subprocess.run(["git", "-C", str(cwd), *IDENT, *args], capture_output=True, text=True)
    if check and done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """`git rebase --continue` commits, so it needs an identity on a machine with none."""
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.test")


def write_rules(repo, rules):
    (repo / ".agents").mkdir(exist_ok=True)
    body = "[merge]\nrules = [" + ", ".join(
        "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in r.items()) + " }" for r in rules) + "]\n"
    (repo / ".agents" / "agent-tools.toml").write_text(body, encoding="utf-8")


REGEN = [PY, "-c", "import pathlib; pathlib.Path('Cargo.lock').write_text('regenerated\\n')"]


@pytest.fixture
def two_branches(tmp_path):
    """Two branches that both add a CHANGELOG entry and change Cargo.lock, one commit each, off one base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n- base\n", encoding="utf-8")
    (repo / "Cargo.lock").write_text("lock base\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".agents\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: base")
    for name in ("one", "two"):
        git(repo, "checkout", "-q", "-b", name, "main")
        (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n## Unreleased\n\n- {name} entry\n- base\n", encoding="utf-8")
        (repo / "Cargo.lock").write_text(f"lock {name}\n", encoding="utf-8")
        git(repo, "commit", "-q", "-am", f"feat: {name}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "one")
    return repo


def test_a_rebase_of_two_branches_is_resolved_by_rules_alone(two_branches, capsys):
    repo = two_branches
    write_rules(repo, [{"path": "CHANGELOG.md", "how": "union"},
                       {"path": "Cargo.lock", "how": "ours-then-run", "run": REGEN}])
    git(repo, "checkout", "-q", "two")
    assert git(repo, "rebase", "main", check=False) is not None
    assert _merge.operation(repo) == "rebase"
    assert _merge.continue_pick(repo, 10) == 0
    out = capsys.readouterr().out
    assert "resolved CHANGELOG.md (union)" in out and "resolved Cargo.lock (ours-then-run)" in out
    assert _merge.operation(repo) is None
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- one entry" in changelog and "- two entry" in changelog and "<<<<" not in changelog
    assert (repo / "Cargo.lock").read_text(encoding="utf-8") == "regenerated\n"
    assert git(repo, "log", "--format=%s", "main..two") == "feat: two"


def test_a_path_no_rule_matches_stops_before_staging_anything(two_branches, capsys):
    repo = two_branches
    write_rules(repo, [{"path": "CHANGELOG.md", "how": "union"}])
    git(repo, "checkout", "-q", "two")
    git(repo, "rebase", "main", check=False)
    assert _merge.continue_pick(repo, 10) == 1
    assert "MANUAL: Cargo.lock" in capsys.readouterr().out
    assert "<<<<<<<" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert git(repo, "diff", "--name-only", "--diff-filter=U").split() == ["CHANGELOG.md", "Cargo.lock"]


def test_a_cherry_pick_is_resolved_too(two_branches, capsys):
    repo = two_branches
    write_rules(repo, [{"path": "CHANGELOG.md", "how": "union"}, {"path": "*.lock", "how": "ours-then-run", "run": REGEN},
                       {"path": "Cargo.lock", "how": "ours-then-run", "run": REGEN}])
    git(repo, "cherry-pick", "two", check=False)
    assert _merge.operation(repo) == "cherry-pick"
    assert _merge.continue_pick(repo, 10) == 0
    assert "- two entry" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert git(repo, "log", "--format=%s", "-1") == "feat: two"


def test_a_file_a_rule_leaves_with_a_marker_is_not_staged(two_branches, capsys):
    repo = two_branches
    # `ours-then-run` with a command that writes a marker: the loop refuses to stage it.
    bad = [PY, "-c", "open('Cargo.lock', 'w').write('<<<<<<< x\\na\\n=======\\nb\\n>>>>>>> y\\n')"]
    write_rules(repo, [{"path": "CHANGELOG.md", "how": "union"}, {"path": "Cargo.lock", "how": "ours-then-run", "run": bad}])
    git(repo, "checkout", "-q", "two")
    git(repo, "rebase", "main", check=False)
    assert _merge.continue_pick(repo, 10) == 1
    assert "a conflict marker remains in Cargo.lock" in capsys.readouterr().out
    assert git(repo, "diff", "--name-only", "--diff-filter=U").split() == ["CHANGELOG.md", "Cargo.lock"]


def test_the_round_limit_is_reported(two_branches, capsys):
    repo = two_branches
    write_rules(repo, [{"path": "*", "how": "union"}])
    git(repo, "checkout", "-q", "two")
    git(repo, "rebase", "main", check=False)
    assert _merge.continue_pick(repo, 0) == 2


def test_nothing_to_continue_is_done(two_branches, capsys):
    assert _merge.continue_pick(two_branches, 3) == 0


def test_an_unknown_how_stops(two_branches, capsys):
    repo = two_branches
    write_rules(repo, [{"path": "*", "how": "magic"}])
    git(repo, "checkout", "-q", "two")
    git(repo, "rebase", "main", check=False)
    assert _merge.continue_pick(repo, 10) == 1
    assert "unknown how 'magic'" in capsys.readouterr().out


# --- the command line ---------------------------------------------------------------------------------------------


def run(*argv):
    return duho.parse(merge_cmd.Merge, [str(a) for a in argv])()


def test_the_command_line_resolves_and_reports(tmp_path, capsys):
    path = put(tmp_path, conflict("a", "b"))
    assert run("union", path) == 0
    assert "hunks 1" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == "top\na\nb\nbottom\n"


def test_the_command_line_dupes_exit_code(tmp_path, capsys):
    long = "x" * 70
    path = put(tmp_path, f"{long}1\n{long}2\n")
    assert run("dupes", path) == 1
    assert run("dupes", put(tmp_path, "ok\n", "ok.md")) == 0


def test_the_command_line_continue_reports_a_bad_rule_file(two_branches, capsys):
    (two_branches / ".agents").mkdir(exist_ok=True)
    (two_branches / ".agents" / "agent-tools.toml").write_text("[merge]\nrulez = []\n", encoding="utf-8")
    assert run("continue", "-C", two_branches) == 2
    assert "unknown key merge.rulez" in capsys.readouterr().err


def test_the_command_line_pub_lock_and_fix_utf8(tmp_path, capsys):
    ours = put(tmp_path, PUB, "o.lock")
    theirs = put(tmp_path, PUB, "t.lock")
    assert run("pub-lock", ours, theirs, tmp_path / "m.lock") == 0
    stray = put(tmp_path, "", "s.md", raw=b"caf\xe9\n")
    assert run("fix-utf8", stray) == 0
    assert stray.read_bytes() == "café\n".encode("utf-8")
