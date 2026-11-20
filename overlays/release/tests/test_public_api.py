"""Tests for overlays/release/repo/tools/public_api.py; the cargo call is replaced."""

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "repo" / "tools" / "public_api.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("public_api_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool():
    return load_tool()


@pytest.fixture
def calls(tool, monkeypatch):
    """Records every command; the output of the fake cargo is `out["text"]`."""
    log = []
    out = {"text": "pub fn a()\r\npub fn b()\n", "code": 0}

    def fake(cmd, cwd):
        log.append((cmd, cwd))
        return out["code"], out["text"]

    monkeypatch.setattr(tool, "run_command", fake)
    return log, out


def repo(tmp_path, cfg):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "build.cfg").write_text(cfg, encoding="utf-8", newline="\n")
    return tmp_path


def run(tool, root, command):
    return tool.main([command, "--root", str(root)])


SNAP = b"pub fn a()\npub fn b()\n"


def test_header_line_three():
    assert TOOL.read_text(encoding="utf-8").splitlines()[2] == (
        "# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/public_api.py, at commit SOURCE."
    )


def test_update_then_check_dir(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a", "b"]\n')
    assert run(tool, root, "update") == 0
    assert capsys.readouterr().out.splitlines() == ["updated a", "updated b"]
    assert (root / "tools/public-api/a.txt").read_bytes() == SNAP
    assert run(tool, root, "check") == 0
    assert capsys.readouterr().out.splitlines() == ["verified a", "verified b"]
    log, _ = calls
    cmd, cwd = log[0]
    assert cmd == ["cargo", "public-api", "--manifest-path", "Cargo.toml", "--package", "a",
                   "--all-features", "-sss", "--color", "never"]
    assert cwd == root.resolve()


def test_check_differs(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\n')
    (root / "tools/public-api").mkdir()
    (root / "tools/public-api/a.txt").write_bytes(b"old\n")
    assert run(tool, root, "check") == 1
    assert capsys.readouterr().err.strip() == (
        "public_api.py: Public API snapshot differs for a; run public_api.py update and review the diff"
    )


def test_check_missing(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\n')
    assert run(tool, root, "check") == 1
    assert capsys.readouterr().err.strip() == (
        "public_api.py: Missing public API snapshot tools/public-api/a.txt; run public_api.py update"
    )


def test_check_stale(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\n')
    run(tool, root, "update")
    (root / "tools/public-api/gone.txt").write_bytes(b"x\n")
    capsys.readouterr()
    assert run(tool, root, "check") == 1
    assert "public_api.py: stale snapshot tools/public-api/gone.txt" in capsys.readouterr().err


def test_file_layout_without_package(tool, calls, tmp_path):
    root = repo(tmp_path, '[public_api]\nlayout = "file"\n')
    assert run(tool, root, "update") == 0
    assert (root / "tools/public-api.txt").read_bytes() == SNAP
    assert run(tool, root, "check") == 0
    cmd, _ = calls[0][0]
    assert "--package" not in cmd


def test_file_layout_one_package_custom_path(tool, calls, tmp_path):
    root = repo(tmp_path, '[public_api]\nlayout = "file"\npackages = ["z"]\npath = "api.txt"\n')
    assert run(tool, root, "update") == 0
    assert (root / "api.txt").read_bytes() == SNAP
    assert "--package" in calls[0][0][0]


def test_file_layout_two_packages_is_error(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\nlayout = "file"\npackages = ["a", "b"]\n')
    assert run(tool, root, "check") == 1
    assert "packages" in capsys.readouterr().err


def test_toolchain_argument_only_when_set(tool, calls, tmp_path):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\ntoolchain = "nightly-2026-09-21"\n')
    run(tool, root, "update")
    assert calls[0][0][0][:2] == ["cargo", "+nightly-2026-09-21"]
    (tmp_path / "x").mkdir()
    root2 = repo(tmp_path / "x", '[public_api]\npackages = ["a"]\n')
    run(tool, root2, "update")
    assert not any(part.startswith("+") for part in calls[0][1][0])


def test_toolchain_from_sdk_lock(tool, calls, tmp_path):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\nsdk = "api rust"\n')
    (root / "tools/sdk.lock").write_text(
        "# group name version platform sha256 url\n"
        "other rust 1.0 any abc http://x\n"
        "api rust nightly-2026-09-21 any abc http://y # c\n"
        "api rust nightly-2027-01-01 any abc http://z\n",
        encoding="utf-8",
    )
    assert run(tool, root, "update") == 0
    assert calls[0][0][0][1] == "+nightly-2026-09-21"


def test_sdk_lock_path_from_sdk_table(tool, calls, tmp_path):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\nsdk = "api rust"\n[sdk]\nlock = "l.txt"\nwhatever = 1\n')
    (root / "l.txt").write_text("api rust 1.99 any a b\n", encoding="utf-8")
    assert run(tool, root, "update") == 0
    assert calls[0][0][0][1] == "+1.99"


def test_sdk_without_row(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\nsdk = "api rust"\n')
    (root / "tools/sdk.lock").write_text("x y 1 any a b\n", encoding="utf-8")
    assert run(tool, root, "update") == 1
    assert "no row for api rust" in capsys.readouterr().err


def test_toolchain_and_sdk_conflict(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\ntoolchain = "stable"\nsdk = "api rust"\n')
    assert run(tool, root, "check") == 1
    assert "public_api.py: build.cfg: public_api:" in capsys.readouterr().err


def test_unknown_key(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackge = ["a"]\n')
    assert run(tool, root, "check") == 1
    assert "public_api.py: build.cfg: public_api.packge: unknown key" in capsys.readouterr().err


def test_no_config_means_no_packages(tool, calls, tmp_path, capsys):
    assert run(tool, tmp_path, "check") == 0
    assert capsys.readouterr().out == ""
    assert calls[0] == []


def test_failing_command(tool, calls, tmp_path, capsys):
    root = repo(tmp_path, '[public_api]\npackages = ["a"]\n')
    calls[1]["code"] = 101
    assert run(tool, root, "update") == 1
    assert capsys.readouterr().err.strip() == "public_api.py: Public API extraction failed for a"
    assert not (root / "tools/public-api/a.txt").exists()


def test_usage_error(tool):
    assert tool.main(["bogus"]) == 2
