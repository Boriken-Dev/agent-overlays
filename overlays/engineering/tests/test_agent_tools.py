"""The two-file configuration and the commit-message rule every agent tool shares."""
from __future__ import annotations

import pytest

from tools_env import no_machine_agent_files  # noqa: F401  (an autouse fixture)

import agent_tools as at

# What each of the two earlier checks refused, and what the user's global instructions forbid.
REFUSED = {
    "model co-author": "feat: x\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\n",
    "lower-case co-author": "fix: y\n\nco-authored-by: GPT-5 <bot@example.test>",
    "copilot co-author": "fix: y\n\nCo-authored-by: Copilot <copilot@example.test>",
    "assistant co-author": "fix: y\n\nCo-Authored-By: The Assistant <a@example.test>",
    "session trailer": "fix: y\n\nClaude-Session: https://claude.ai/code/session_01PXoRJQ2BXTfPkkNNkwS3rr",
    "another tool's session trailer": "fix: y\n\nCodex-Session: abc123",
    "session url in prose": "fix: y\n\nSee https://claude.ai/code/session_abc for the run.",
    "generated with footer": "fix: y\n\nGenerated with [Claude Code](https://claude.com/claude-code)",
    "robot footer": "fix: y\n\n\U0001F916 Generated with Claude Code",
    "generated with, plain": "fix: y\n\nGenerated with Codex",
}
ALLOWED = {
    "human co-author": "feat: x\n\nCo-authored-by: Jane Doe <jane@example.test>",
    "two human co-authors": "feat: x\n\nCo-Authored-By: Ana <ana@example.test>\nCo-Authored-By: Luis Perez <luis@example.test>",
    "session word in prose": "fix: the session: handling\n\nSession: tokens now expire.",
    "generated with in a sentence": "docs: the table was generated with sed and checked by hand",
    "a human with a model-ish name": "feat: x\n\nCo-authored-by: Modesto Rivera <modesto@example.test>",
    "plain": "chore: bump",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_message_is_refused(name):
    found = at.check_message(REFUSED[name])
    assert found, name
    assert all(line.startswith("line ") for line in found)


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_the_message_passes(name):
    assert at.check_message(ALLOWED[name]) == []


def test_a_hit_names_its_line():
    found = at.check_message("fix: y\n\nbody\nClaude-Session: x")
    assert len(found) == 1 and found[0].startswith("line 4: a *-Session: trailer")


def test_forbidden_trailers_are_compiled_patterns():
    assert all(hasattr(p, "search") for p in at.forbidden_trailers())


def write_project(tmp_path, text):
    (tmp_path / ".agents").mkdir(exist_ok=True)
    (tmp_path / ".agents" / "agent-tools.toml").write_text(text, encoding="utf-8")


def test_a_project_adds_its_own_pattern(tmp_path):
    write_project(tmp_path, '[repo_guard]\nforbidden = ["^wip:"]\n')
    config = at.load(tmp_path)
    assert config.check_message("wip: not yet")
    assert config.check_message("fix: ready") == []


def test_a_bad_extra_pattern_is_named(tmp_path):
    write_project(tmp_path, '[repo_guard]\nforbidden = ["("]\n')
    with pytest.raises(at.ConfigError, match="forbidden"):
        at.load(tmp_path).check_message("x")


def write_both(tmp_path, project, local):
    write_project(tmp_path, project)
    (tmp_path / "local.toml").write_text(local, encoding="utf-8")
    return at.load(tmp_path, local=tmp_path / "local.toml")


def test_the_two_files_merge(tmp_path):
    config = write_both(
        tmp_path,
        '[repo_guard]\nmain = "trunk"\n[gate]\nsteps = [{ name = "fmt", run = "cargo fmt", slot = true }]\n',
        '[identity]\nname = "A"\nemail = "a@example.test"\n[gate]\nlock = "x.lock"\n',
    )
    assert config.table("gate") == {"steps": [{"name": "fmt", "run": "cargo fmt", "slot": True}], "lock": "x.lock"}
    assert config.identity() == ("A", "a@example.test")
    assert config.get("repo_guard", "main") == "trunk"
    assert config.table("worktrees") == {}


def test_a_missing_file_is_empty(tmp_path):
    config = at.load(tmp_path, local=tmp_path / "none.toml")
    assert config.table("identity") == {}


def test_a_required_key_names_the_file_and_key(tmp_path):
    config = write_both(tmp_path, "", '[identity]\nname = "A"\n')
    with pytest.raises(at.ConfigError) as caught:
        config.identity()
    assert "local.toml" in str(caught.value) and "[identity] email" in str(caught.value)


def test_a_project_table_in_the_machine_file_is_unknown(tmp_path):
    with pytest.raises(at.ConfigError, match=r"unknown table \[merge\]"):
        write_both(tmp_path, "", "[merge]\nrules = []\n")


def test_an_unknown_key_is_refused_with_its_path(tmp_path):
    with pytest.raises(at.ConfigError, match=r"unknown key repo_guard\.mian"):
        write_both(tmp_path, '[repo_guard]\nmian = "x"\n', "")
    with pytest.raises(at.ConfigError, match=r"unknown key gate\.steps\[0\]\.slott"):
        write_both(tmp_path, '[gate]\nsteps = [{ name = "a", run = "b", slott = true }]\n', "")


def test_a_wrong_type_is_refused(tmp_path):
    with pytest.raises(at.ConfigError, match="must be an integer"):
        write_both(tmp_path, "", "[build_slot]\nlimit = true\n")
    with pytest.raises(at.ConfigError, match="must be a list"):
        write_both(tmp_path, '[gate]\nrestore = "x"\n', "")


def test_a_toml_error_names_the_file(tmp_path):
    with pytest.raises(at.ConfigError, match="local.toml: cannot be read"):
        write_both(tmp_path, "", "[identity\n")


def test_the_environment_names_the_machine_file(tmp_path, monkeypatch):
    target = tmp_path / "mine.toml"
    target.write_text('[identity]\nname = "Z"\nemail = "z@example.test"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_LOCAL", str(target))
    assert at.load(None).identity() == ("Z", "z@example.test")
