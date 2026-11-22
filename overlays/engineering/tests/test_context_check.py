"""`context-check`: another tool's context file, a misnamed one and a private one are reported where a commit would hold
them, and the standard layout passes."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "skills" / "context-check" / "context_check.py"
_spec = importlib.util.spec_from_file_location("context_check_cmd", MODULE)
context_check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = context_check  # duho resolves field annotations through sys.modules
_spec.loader.exec_module(context_check)

GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

STANDARD = {
    "README.md": "# project\n",
    "AGENTS.md": "orientation\n",
    "AGENTS.team.md": "a shared addition\n",
    "docs/AGENTS.md": "about docs\n",
    "src/lib/AGENTS.md": "about the library\n",
    "src/main.py": "print(1)\n",
}


@pytest.fixture(autouse=True)
def default_names(monkeypatch):
    monkeypatch.delenv("AGENTS_DIR", raising=False)
    monkeypatch.delenv("AGENTS_CONTEXT", raising=False)


def make(root: Path, files: "dict[str, str]", ignore: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=GIT_ENV, capture_output=True)
    if ignore:
        (root / ".gitignore").write_text(ignore, encoding="utf-8", newline="\n")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root


def run(*argv):
    return duho.parse(context_check.ContextCheck, [str(a) for a in argv])()


def kinds(root: Path, *allow: str) -> "list[tuple[str, str]]":
    return [(kind, path) for kind, path, _ in context_check.check(context_check.listing(root), list(allow))]


def test_the_standard_layout_passes(tmp_path, capsys):
    root = make(tmp_path / "repo", STANDARD)
    assert run(root) == 0
    assert "standard places" in capsys.readouterr().out


def test_a_claude_md_that_a_commit_would_hold_is_flagged_with_where_it_goes(tmp_path, capsys):
    root = make(tmp_path / "repo", {**STANDARD, "CLAUDE.md": "rules\n"})
    assert run(root) == 1
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "CLAUDE.md" in line)
    assert line.startswith("foreign")
    assert "Claude Code" in line and "move the text into AGENTS.md" in line


def test_the_same_file_in_a_directory_names_that_directorys_agents_md(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, "docs/GEMINI.md": "x\n"})
    [(kind, path, advice)] = context_check.check(context_check.listing(root))
    assert (kind, path) == ("foreign", "docs/GEMINI.md")
    assert "docs/AGENTS.md" in advice


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "GEMINI.md", "QWEN.md", "AGENT.md", ".cursorrules",
        ".cursor/rules/style.mdc", ".windsurfrules", ".clinerules", ".github/copilot-instructions.md",
        "pkg/CLAUDE.md", "claude.md",
    ],
)
def test_each_other_tools_file_is_foreign(tmp_path, path):
    root = make(tmp_path / "repo", {"AGENTS.md": "x\n", path: "x\n"})
    assert kinds(root) == [("foreign", path)]


def test_a_file_the_repository_ignores_is_not_reported(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, "CLAUDE.md": "x\n", ".claude/CLAUDE.md": "x\n"}, ignore="CLAUDE*\n.claude\n")
    assert kinds(root) == []


def test_the_private_working_area_and_a_local_file_are_flagged_when_they_would_be_committed(tmp_path):
    root = make(
        tmp_path / "repo",
        {**STANDARD, ".agents/AGENTS.md": "mine\n", ".agents/kb/x.md": "mine\n", "AGENTS.local.md": "mine\n", "docs/AGENTS.a.local.md": "x\n"},
    )
    assert kinds(root) == [
        ("private", ".agents/AGENTS.md"),
        ("private", ".agents/kb/x.md"),
        ("private", "AGENTS.local.md"),
        ("private", "docs/AGENTS.a.local.md"),
    ]


def test_the_private_working_area_when_ignored_passes(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, ".agents/AGENTS.md": "mine\n", "AGENTS.local.md": "mine\n"}, ignore=".agents\n*.local.md\n")
    assert kinds(root) == []


def test_a_linked_agents_directory_that_is_committed_is_flagged(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, ".agents": "../private\n"})  # git stores a link as its target's text
    assert kinds(root) == [("private", ".agents")]


@pytest.mark.parametrize("name", ["agents.md", "Agents.md", "AGENTS.MD", "AGENTS.txt", "Agents.local.md", "agents.team.md"])
def test_a_misnamed_context_file_is_flagged_and_the_name_is_the_rule(tmp_path, name):
    # Alone in its folder, so a file system that folds case holds only the one.
    root = make(tmp_path / "repo", {"README.md": "x\n", "docs/" + name: "x\n"})
    # `Agents.local.md` is not the user's own file by its name: it is the case that is wrong.
    assert kinds(root) == [("name", "docs/" + name)]


def test_names_that_are_not_context_files_are_left_alone(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, "agents-notes.md": "x\n", "docs/agents/index.md": "x\n", "AGENTS_TEMPLATE.md": "x\n"})
    assert kinds(root) == []


def test_allow_leaves_out_a_path_the_project_chose_to_keep(tmp_path):
    root = make(tmp_path / "repo", {**STANDARD, "CLAUDE.md": "@AGENTS.md\n", "docs/GEMINI.md": "x\n"})
    assert kinds(root, "CLAUDE.md") == [("foreign", "docs/GEMINI.md")]
    assert kinds(root, "CLAUDE.md", "docs/*") == []
    assert run(root, "--allow", "CLAUDE.md", "--allow", "docs/*") == 0


def test_the_variables_move_the_private_directory_and_the_names(tmp_path, monkeypatch):
    root = make(tmp_path / "repo", {"AGENTS.md": "x\n", ".agents/AGENTS.md": "x\n", ".ai/notes.md": "x\n", "CONTEXT.local.md": "x\n"})
    assert kinds(root) == [("private", ".agents/AGENTS.md")]
    monkeypatch.setenv("AGENTS_DIR", ".ai")
    monkeypatch.setenv("AGENTS_CONTEXT", os.pathsep.join(["CONTEXT.md", "CONTEXT.*.md"]))
    assert kinds(root) == [("private", ".ai/notes.md"), ("private", "CONTEXT.local.md")]


def test_a_name_pattern_is_a_star_over_any_run_of_characters():
    assert context_check.matches("AGENTS.*.md", "AGENTS.a.b.md")
    assert not context_check.matches("AGENTS.*.md", "AGENTS.md")
    assert context_check.matches("AGENTS*.md", "AGENTS.md")
    assert context_check.is_local("AGENTS.local.md") and context_check.is_local("AGENTS.x.local.md")
    assert not context_check.is_local(".local.md") and not context_check.is_local("AGENTS.local")


def test_without_git_every_file_counts_and_the_usual_build_folders_are_skipped(tmp_path):
    root = tmp_path / "plain"
    for name in ["AGENTS.md", "CLAUDE.md", "node_modules/pkg/CLAUDE.md", "target/x/GEMINI.md"]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n", encoding="utf-8")
    assert kinds(root) == [("foreign", "CLAUDE.md")]


def test_a_directory_with_no_files_cannot_be_checked(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    assert run(root) == 3
    assert "CANNOT CHECK" in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path / "missing")
