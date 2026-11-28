"""Adopting a real ``.agents`` into a directory-layout store is all or nothing."""
import os

import pytest

from _link import link_project, project_store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_STORE_LAYOUT", "dir")
    monkeypatch.delenv("AGENTS_STORE_DIR", raising=False)
    agents = tmp_path / "agents"
    agents.mkdir()
    project = tmp_path / "app"
    (project / ".agents" / "plans").mkdir(parents=True)
    (project / ".gitignore").write_text(".agents\n", encoding="utf-8")
    return agents, project


def _put(root, rel, text="x\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _symlink_or_skip(link, target):
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args if args else msg)


def test_a_dangling_symlink_is_skipped_with_a_warning_and_the_rest_is_adopted(env):
    agents, project = env
    _put(project / ".agents", "plans/a.md", "plan\n")
    _put(project / ".agents", "AGENTS.md", "notes\n")
    _symlink_or_skip(project / ".agents" / "cache-link", project / "missing-target")
    log = Logger()
    link_project(project, agents, logger=log)
    store = project_store(agents, "app")
    assert (store / "plans" / "a.md").read_text(encoding="utf-8") == "plan\n"
    assert (store / "AGENTS.md").is_file() and not (store / "cache-link").exists()
    assert os.path.islink(str(project / ".agents"))
    assert any("dangling" in line and "cache-link" in line for line in log.lines)


def test_a_failed_copy_leaves_the_project_and_the_store_unchanged(env, monkeypatch):
    import _link

    agents, project = env
    _put(project / ".agents", "plans/a.md")
    _put(project / ".agents", "plans/b.md")
    real = _link.shutil.copy2
    calls = []

    def flaky(src, dst, *a, **k):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(_link.shutil, "copy2", flaky)
    with pytest.raises(SystemExit):
        link_project(project, agents)
    assert (project / ".agents" / "plans" / "a.md").is_file()
    assert (project / ".agents" / "plans" / "b.md").is_file()
    assert not os.path.islink(str(project / ".agents"))
    store = project_store(agents, "app")
    assert not any(p.is_file() and p.name != ".gitkeep" for p in store.rglob("*"))
    assert not [p for p in store.parent.iterdir() if p.name.startswith(".incoming")]


def test_a_refusal_says_the_project_copy_is_a_strict_subset_of_the_store(env):
    agents, project = env
    store = project_store(agents, "app")
    _put(store, "plans/a.md", "a\n")
    _put(store, "plans/b.md", "b\n")
    _put(project / ".agents", "plans/a.md", "a\n")
    with pytest.raises(SystemExit) as exc:
        link_project(project, agents)
    assert "strict subset of the store" in str(exc.value)


def test_a_refusal_says_the_store_is_a_strict_subset_and_prefer_project_resolves_it(env):
    agents, project = env
    store = project_store(agents, "app")
    _put(store, "plans/a.md", "a\n")
    _put(project / ".agents", "plans/a.md", "a\n")
    _put(project / ".agents", "plans/b.md", "b\n")
    with pytest.raises(SystemExit) as exc:
        link_project(project, agents)
    assert "store is a strict subset" in str(exc.value) and "--prefer-project" in str(exc.value)

    link_project(project, agents, prefer_project=True)
    assert (store / "plans" / "a.md").is_file() and (store / "plans" / "b.md").is_file()
    assert os.path.islink(str(project / ".agents"))
    assert (project / ".agents.bak" / "plans" / "b.md").is_file()


def test_a_diverged_refusal_counts_each_side(env):
    agents, project = env
    store = project_store(agents, "app")
    _put(store, "only_store.md")
    _put(store, "both.md", "one\n")
    _put(project / ".agents", "only_project.md")
    _put(project / ".agents", "both.md", "two\n")
    with pytest.raises(SystemExit) as exc:
        link_project(project, agents)
    msg = str(exc.value)
    assert "1 only in the project" in msg and "1 only in the store" in msg and "1 differ" in msg
