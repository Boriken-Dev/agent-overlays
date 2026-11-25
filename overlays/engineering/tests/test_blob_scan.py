"""`blob-scan`: what a scanner may refuse is flagged in any ref, text and images pass, and the exit code says which."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "blob-scan" / "blob_scan.py"
GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *IDENT, *args], check=True, capture_output=True, env=GIT_ENV)


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(repo), *args], capture_output=True, text=True, env=GIT_ENV)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("plain text\n")
    # A 1x1 PNG header is media, not a flag.
    (root / "i.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(64)))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_text_and_images_pass(repo: Path) -> None:
    done = scan(repo)
    assert done.returncode == 0, done.stdout
    assert done.stdout.strip().splitlines()[-1].startswith("PASS: 0 flagged")


def test_an_archive_anywhere_in_history_fails(repo: Path) -> None:
    (repo / "x.bin").write_bytes(b"PK\x03\x04" + bytes(100))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add archive")
    git(repo, "rm", "-q", "x.bin")
    git(repo, "commit", "-q", "-m", "remove it")
    done = scan(repo)
    assert done.returncode == 1
    assert "FLAG zip: 1" in done.stdout
    assert "FAIL: 1 flagged" in done.stdout


def test_a_key_file_by_name_and_a_private_key_in_text_fail(repo: Path) -> None:
    (repo / "k.p12").write_text("not really")
    body = "\n".join(["-----BEGIN PRIVATE KEY-----", "A" * 64, "B" * 64, "-----END PRIVATE KEY-----", ""])
    (repo / "pem.txt").write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "keys")
    done = scan(repo)
    assert done.returncode == 1
    assert "FLAG key or keystore file: 1" in done.stdout
    assert "FLAG private key in text: 1" in done.stdout


def test_refs_limits_the_scan(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "e.exe").write_bytes(b"MZ" + bytes(200))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "exe")
    git(repo, "checkout", "-q", "main")
    assert scan(repo, "--refs", "main").returncode == 0
    assert scan(repo, "--refs", "side").returncode == 1
    assert scan(repo).returncode == 1


def test_no_argument_is_a_usage_error() -> None:
    done = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 2
    assert "Usage: blob_scan.py" in done.stdout
