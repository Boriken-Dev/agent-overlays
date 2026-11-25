"""`leak-check-history`: a marker in an old message, path or blob is found, a fork's upstream authors are labelled, and a
missing pattern file is not a pass."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "leak-check" / "leak_check_history.py"
BASE = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def commit(repo: Path, who: str, message: str) -> None:
    env = dict(BASE, GIT_AUTHOR_NAME=who, GIT_AUTHOR_EMAIL=f"{who}@example.test",
               GIT_COMMITTER_NAME=who, GIT_COMMITTER_EMAIL=f"{who}@example.test")
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], check=True, env=env)


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, env=BASE)
    return repo


def run(root: Path, patterns: Path | None, *extra: str) -> subprocess.CompletedProcess:
    args = [sys.executable, "-I", str(SCRIPT), str(root), *extra]
    env = dict(BASE, AGENTS_AUDIT_PATTERNS=str(patterns or root / "missing.json"), HOME=str(root), USERPROFILE=str(root))
    return subprocess.run(args, capture_output=True, text=True, env=env)


def patterns(tmp_path: Path, **data) -> Path:
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"personal": ["Sekrit"], "ours": ["owner"], **data}))
    return path


def test_a_marker_removed_from_the_tree_is_still_found_in_history(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo = make_repo(repos, "mine")
    (repo / "a.txt").write_text("path /home/sekrit/x\n")
    commit(repo, "owner", "add")
    (repo / "a.txt").write_text("clean\n")
    commit(repo, "owner", "clean up")
    done = run(repos, patterns(tmp_path))
    assert done.returncode == 1, done.stdout
    assert "HITS" in done.stdout and "blob [personal:Sekrit]" in done.stdout


def test_a_clean_history_passes(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo = make_repo(repos, "mine")
    (repo / "a.txt").write_text("clean\n")
    commit(repo, "owner", "add")
    done = run(repos, patterns(tmp_path))
    assert done.returncode == 0, done.stdout
    assert "CLEAN  mine" in done.stdout


def test_a_hit_by_someone_else_is_upstream_and_not_ours(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo = make_repo(repos, "fork")
    (repo / "a.txt").write_text("sekrit\n")
    commit(repo, "stranger", "their change")
    done = run(repos, patterns(tmp_path))
    assert done.returncode == 0, done.stdout
    assert "UPSTREAM" in done.stdout
    assert "repos with OUR leaks: 0" in done.stdout


def test_a_message_hit_and_the_name_filter(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    a, b = make_repo(repos, "a"), make_repo(repos, "b")
    (a / "f").write_text("x\n")
    commit(a, "owner", "mentions Sekrit")
    (b / "f").write_text("x\n")
    commit(b, "owner", "fine")
    assert run(repos, patterns(tmp_path)).returncode == 1
    assert run(repos, patterns(tmp_path), "b").returncode == 0


def test_a_missing_pattern_file_is_not_a_pass(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    make_repo(repos, "mine")
    done = run(repos, None)
    assert done.returncode == 3
    assert "no pattern file" in done.stderr
