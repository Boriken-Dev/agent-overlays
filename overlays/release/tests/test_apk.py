"""Tests for overlays/release/repo/tools/apk.py; every APK is built in a tmp_path."""

import base64
import importlib.util
import zipfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "repo" / "tools" / "apk.py"
SECRET = b"sk_live_ABCDEF0123456789"
PATTERN = "sk_live_[A-Za-z0-9]+"


def load_tool():
    spec = importlib.util.spec_from_file_location("apk_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool():
    return load_tool()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def make_apk(path: Path, entries: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def repo(tmp_path, cfg):
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "build.cfg").write_text(cfg, encoding="utf-8", newline="\n")
    return tmp_path


def forbid_cfg(control=SECRET, extra=""):
    return (
        f'[apk]\n{extra}\n[[apk.forbid]]\nstring = "{PATTERN}"\n'
        f'control_base64 = "{b64(control)}"\nname = "live-key"\n'
    )


def run(tool, root, *args):
    return tool.main(["check", "--root", str(root), *args])


def test_header_line_three():
    assert TOOL.read_text(encoding="utf-8").splitlines()[2] == (
        "# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/apk.py, at commit SOURCE."
    )


def test_planted_secret_fails_and_is_never_printed(tool, tmp_path, capsys):
    root = repo(tmp_path, forbid_cfg())
    apk = make_apk(tmp_path / "bad.apk", {"assets/cfg.txt": b"key=" + SECRET + b"\n"})
    assert run(tool, root, str(apk)) == 1
    captured = capsys.readouterr()
    assert "assets/cfg.txt: a match of 24 bytes for forbidden live-key" in captured.err
    assert "reading bad.apk" in captured.out
    assert "PASS" not in captured.out
    assert SECRET.decode() not in captured.out + captured.err


def test_clean_apk_passes(tool, tmp_path, capsys):
    root = repo(tmp_path, forbid_cfg())
    apk = make_apk(tmp_path / "ok.apk", {"a.txt": b"nothing here"})
    assert run(tool, root, str(apk)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["reading ok.apk", "scanned 1 entries", "PASS forbidden strings"]


def test_negative_control_must_match(tool, tmp_path, capsys):
    root = repo(tmp_path, forbid_cfg(control=b"harmless"))
    apk = make_apk(tmp_path / "ok.apk", {"a.txt": b"x"})
    assert run(tool, root, str(apk)) == 2
    assert "the check no longer detects its planted sample; fix it first" in capsys.readouterr().err


def test_empty_control_is_refused(tool, tmp_path, capsys):
    root = repo(tmp_path, f'[[apk.forbid]]\nstring = "{PATTERN}"\n')
    apk = make_apk(tmp_path / "ok.apk", {"a.txt": b"x"})
    assert run(tool, root, str(apk)) == 2
    assert "planted sample" in capsys.readouterr().err


def test_allow_flag_reports_but_exits_zero(tool, tmp_path, capsys):
    root = repo(tmp_path, forbid_cfg(extra='allow_flag = "--allow-sideload-key"'))
    apk = make_apk(tmp_path / "bad.apk", {"k.bin": SECRET})
    assert run(tool, root, "--allow-sideload-key", str(apk)) == 0
    err = capsys.readouterr().err
    assert "k.bin: a match of 24 bytes for forbidden live-key" in err
    assert "must never go to a store" in err
    assert SECRET.decode() not in err
    assert run(tool, root, str(apk)) == 1


def test_allow_flag_does_not_hide_other_problems(tool, tmp_path):
    cfg = forbid_cfg(extra='allow_flag = "--allow-sideload-key"\nabis = ["arm64-v8a"]')
    root = repo(tmp_path, cfg)
    apk = make_apk(tmp_path / "bad.apk", {"k.bin": SECRET})
    assert run(tool, root, "--allow-sideload-key", str(apk)) == 1


def test_oversize_entry_is_skipped_and_listed(tool, tmp_path, capsys):
    root = repo(tmp_path, forbid_cfg(extra="max_entry_bytes = 10"))
    apk = make_apk(tmp_path / "big.apk", {"big.bin": SECRET})
    assert run(tool, root, str(apk)) == 0
    out = capsys.readouterr().out
    assert "skipped big.bin" in out and "scanned 0 entries" in out


def manifest(*names):
    return "".join(names).encode("utf-16-le")


def test_permissions(tool, tmp_path, capsys):
    cfg = ('[apk.permissions]\nwant = ["android.permission.INTERNET"]\n'
           'unwanted = ["android.permission.CAMERA"]\n')
    root = repo(tmp_path, cfg)
    good = make_apk(tmp_path / "g.apk", {"AndroidManifest.xml": manifest("android.permission.INTERNET")})
    assert run(tool, root, str(good)) == 0
    assert "PASS permissions" in capsys.readouterr().out
    bad = make_apk(tmp_path / "b.apk", {
        "AndroidManifest.xml": manifest("android.permission.CAMERA")})
    assert run(tool, root, str(bad)) == 1
    err = capsys.readouterr().err
    assert "wanted permission missing: android.permission.INTERNET" in err
    assert "unwanted permission present: android.permission.CAMERA" in err


def test_permissions_utf8_manifest(tool, tmp_path):
    root = repo(tmp_path, '[apk.permissions]\nwant = ["android.permission.INTERNET"]\n')
    apk = make_apk(tmp_path / "a.apk", {"AndroidManifest.xml": b"android.permission.INTERNET"})
    assert run(tool, root, str(apk)) == 0


def test_permissions_without_manifest(tool, tmp_path, capsys):
    root = repo(tmp_path, '[apk.permissions]\nwant = ["x"]\n')
    apk = make_apk(tmp_path / "a.apk", {"a": b"1"})
    assert run(tool, root, str(apk)) == 1
    assert "AndroidManifest.xml: missing" in capsys.readouterr().err


def test_abis_exact(tool, tmp_path, capsys):
    root = repo(tmp_path, '[apk]\nabis = ["arm64-v8a"]\n')
    good = make_apk(tmp_path / "g.apk", {"lib/arm64-v8a/liba.so": b"1"})
    assert run(tool, root, str(good)) == 0
    bad = make_apk(tmp_path / "b.apk", {
        "lib/x86_64/liba.so": b"12345", "lib/x86_64/libb.so": b"123"})
    assert run(tool, root, str(bad)) == 1
    err = capsys.readouterr().err
    assert "surplus ABI x86_64: 2 libs, 8 bytes" in err
    assert "missing ABI arm64-v8a" in err


def test_libs_required_in_every_abi(tool, tmp_path, capsys):
    root = repo(tmp_path, '[apk]\nlibs = ["libapp.so"]\n')
    apk = make_apk(tmp_path / "a.apk", {
        "lib/arm64-v8a/libapp.so": b"1", "lib/x86_64/libother.so": b"1"})
    assert run(tool, root, str(apk)) == 1
    err = capsys.readouterr().err
    assert "lib/x86_64/libapp.so missing" in err
    assert "arm64-v8a" not in err


def test_unknown_key(tool, tmp_path, capsys):
    root = repo(tmp_path, '[apk]\nforbd = []\n')
    apk = make_apk(tmp_path / "a.apk", {"a": b"1"})
    assert run(tool, root, str(apk)) == 1
    assert "apk.py: build.cfg: apk.forbd: unknown key" in capsys.readouterr().err


def test_unknown_key_in_forbid_entry(tool, tmp_path, capsys):
    root = repo(tmp_path, '[[apk.forbid]]\nstring = "x"\nbogus = 1\n')
    assert run(tool, root, "a.apk") == 1
    assert "apk.forbid[0].bogus: unknown key" in capsys.readouterr().err


def test_no_apk_is_usage(tool, tmp_path, capsys):
    assert run(tool, repo(tmp_path, "")) == 2
    assert "no APK" in capsys.readouterr().err


def test_missing_apk_file(tool, tmp_path, capsys):
    missing = tmp_path / "nope.apk"
    assert run(tool, repo(tmp_path, ""), str(missing)) == 2
    assert str(missing) in capsys.readouterr().err
