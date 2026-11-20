"""build.cfg is one file read by build.py and by the tools beside it: their tables must agree."""

from __future__ import annotations

import pytest

from conftest import load_tool

FULL = """
[tools]
files = ["build.py", "public_api.py", "apk.py"]

[sdk]
lock = "tools/sdk.lock"

[public_api]
packages = ["a", "b"]
sdk = "api rust"
layout = "dir"
path = "tools/public-api"
args = ["--all-features"]
cargo = "cargo"

[apk]
max_entry_bytes = 1000
allow_flag = "--allow-sideload-key"
abis = ["arm64-v8a"]
libs = ["libapp.so"]

[apk.permissions]
want = ["android.permission.INTERNET"]
unwanted = ["android.permission.CAMERA"]

[[apk.forbid]]
string = "secret"
control_base64 = "c2VjcmV0"
name = "a secret"
"""


@pytest.fixture(scope="module")
def public_api():
    return load_tool("public_api.py", "public_api_tool")


@pytest.fixture(scope="module")
def apk():
    return load_tool("apk.py", "apk_tool")


def test_build_py_accepts_every_key_the_other_tools_read(build):
    cfg = build.parse_config(FULL)
    assert cfg.public_api.packages == ["a", "b"] and cfg.apk.forbid[0].name == "a secret"
    assert cfg.apk.permissions.want == ["android.permission.INTERNET"]


def test_the_other_tools_accept_the_same_file_and_read_the_same_values(build, public_api, apk):
    import tomllib

    raw = tomllib.loads(FULL)
    cfg = build.parse_config(FULL)
    pub = public_api.settings(raw)
    for key in ("packages", "sdk", "layout", "path", "args", "cargo", "toolchain"):
        expected = getattr(cfg.public_api, key)
        assert pub[key] == (expected if expected != "" or key != "path" else pub[key]), key
    mine = apk.settings(raw["apk"])
    assert mine["abis"] == cfg.apk.abis and mine["libs"] == cfg.apk.libs and mine["allow_flag"] == cfg.apk.allow_flag
    assert mine["max_entry_bytes"] == cfg.apk.max_entry_bytes
    assert mine["permissions"] == {"want": cfg.apk.permissions.want, "unwanted": cfg.apk.permissions.unwanted}


def test_every_default_of_the_other_tools_is_the_one_build_py_prints(build, public_api, apk):
    cfg = build.parse_config("")
    pub = public_api.settings({})
    for key in ("packages", "toolchain", "sdk", "layout", "args", "cargo"):
        assert pub[key] == getattr(cfg.public_api, key), key
    assert pub["path"] == "tools/public-api"
    mine = apk.settings({})
    assert mine["max_entry_bytes"] == cfg.apk.max_entry_bytes and mine["allow_flag"] == cfg.apk.allow_flag
    assert mine["abis"] == cfg.apk.abis and mine["libs"] == cfg.apk.libs and mine["forbid"] == []


def test_a_key_neither_side_knows_is_refused_by_both(build, public_api, apk):
    import tomllib

    for text, table in (("[public_api]\nbogus = 1\n", "public_api"), ("[apk]\nbogus = 1\n", "apk")):
        with pytest.raises(build.Problem, match=f"{table}.bogus: unknown key"):
            build.parse_config(text)
    with pytest.raises(public_api.Problem, match="public_api.bogus: unknown key"):
        public_api.settings(tomllib.loads("[public_api]\nbogus = 1\n"))
    with pytest.raises(apk.Problem, match="apk.bogus: unknown key"):
        apk.settings({"bogus": 1})
