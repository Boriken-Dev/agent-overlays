"""The native keys of `.env` (VSINSTALLDIR, WINDOWSSDKDIR, LIBCLANG_PATH, SDK_HOME, ...) in `sdk install` and `release build`.

No Visual Studio is needed: the directories are made here. One test points the keys at the real ones of the machine
it runs on, reading them with vswhere and the standard locations, and is skipped where they are not there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import init_repo
from test_build_deps import ctx_for
from test_build_release import CFG as RELEASE_CFG, Stub
from test_build_sdk import good_rows, lock_text, machine  # noqa: F401  (a fixture)

NATIVE = ("VSINSTALLDIR", "VCTOOLSINSTALLDIR", "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "LIBCLANG_PATH", "LLVM_DIR", "SDK_HOME")


@pytest.fixture(autouse=True)
def clean_environment(build, monkeypatch):
    for key in build.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BUILD_SDK_HOME", raising=False)


def make(path: Path, *names: str) -> Path:
    """A directory holding each named file or folder (a name ending in / is a folder)."""
    path.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name.endswith("/"):
            (path / name).mkdir(parents=True, exist_ok=True)
        else:
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(b"x")
    return path


@pytest.fixture
def machine_dirs(tmp_path):
    """Fake Visual Studio, Windows SDK and LLVM directories, outside the repository."""
    root = tmp_path / "machine"
    vs = make(root / "BuildTools", "VC/Tools/MSVC/14.44.1/bin/")
    tools = make(vs / "VC" / "Tools" / "MSVC" / "14.44.1", "bin/", "include/", "lib/")
    kit = make(root / "Kits10", "Include/10.0.1.0/", "Lib/10.0.1.0/")
    llvm = make(root / "LLVM", "bin/clang.exe", "bin/libclang.dll")
    return {
        "VSINSTALLDIR": str(vs),
        "VCTOOLSINSTALLDIR": str(tools),
        "WINDOWSSDKDIR": str(kit),
        "WINDOWSSDKVERSION": "10.0.1.0",
        "LIBCLANG_PATH": str(llvm / "bin"),
        "LLVM_DIR": str(llvm),
    }


def env_local(root: Path, values: dict[str, str], name: str = ".env.local") -> None:
    (root / name).write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


@pytest.fixture
def repo(build, tmp_path):
    root = init_repo(tmp_path / "repo")
    ctx_for(build, root, RELEASE_CFG)
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["a"]\n[workspace.package]\nversion = "1.2.3"\n', encoding="utf-8")
    return root


# --- reading and checking the keys ---------------------------------------------------------------------------------------


def test_the_native_keys_are_in_the_catalogue_and_are_not_build_arguments(build):
    assert set(build.NATIVE_KEYS) == set(NATIVE)
    for key in (*NATIVE, "MSVC_SPLAT_DIR"):
        assert key in build.ENV_KEYS and not build.ENV_KEYS[key].arg, key


def test_the_layers_win_in_order_and_a_key_of_another_tool_is_left_alone(build, repo, machine_dirs, tmp_path):
    other = make(tmp_path / "other", "VC/")
    env_local(repo, {"VSINSTALLDIR": machine_dirs["VSINSTALLDIR"]}, ".env")
    (repo / ".env").write_text((repo / ".env").read_text() + "SOMETHING_ELSE=1\nnot a pair\n", encoding="utf-8")
    env_local(repo, {"VSINSTALLDIR": str(other)})
    ctx = build.Ctx(repo)
    assert build.native_settings(ctx, {}, [])["VSINSTALLDIR"] == (str(other), ".env.local")
    assert build.native_settings(ctx, {"VSINSTALLDIR": machine_dirs["VSINSTALLDIR"]}, [])["VSINSTALLDIR"][1] == "environment"
    assert build.native_settings(ctx, {}, [f"VSINSTALLDIR={other}"])["VSINSTALLDIR"][1] == "--env"
    assert "SOMETHING_ELSE" not in build.native_settings(ctx, {}, [])


def test_a_path_that_is_not_there_or_not_the_right_directory_fails_naming_the_key_and_where_it_was_set(build, repo, machine_dirs, tmp_path):
    ctx = build.Ctx(repo)
    empty = make(tmp_path / "empty")
    cases = {
        "VSINSTALLDIR": (str(tmp_path / "missing"), "is not a directory"),
        "VCTOOLSINSTALLDIR": (str(empty), "holds no bin, no include, no lib"),
        "WINDOWSSDKDIR": (str(empty), "holds no Include, no Lib"),
        "LIBCLANG_PATH": (str(empty), "holds no libclang.*"),
        "LLVM_DIR": (str(empty), "holds no bin/clang*"),
        "WINDOWSSDKVERSION": ("ten", "not a Windows SDK version"),
    }
    for key, (value, message) in cases.items():
        env_local(repo, {key: value})
        with pytest.raises(build.Problem) as error:
            build.native_settings(ctx, {}, [])
        assert str(error.value).startswith(f"{key} (.env.local): ") and message in str(error.value), key
    env_local(repo, {"SDK_HOME": str(repo / "Cargo.toml")})
    with pytest.raises(build.Problem, match=r"SDK_HOME \(\.env\.local\): .* exists and is not a directory"):
        build.native_settings(ctx, {}, [])
    # A folder that does not exist yet is fine: `sdk install` makes it.
    env_local(repo, {"SDK_HOME": str(tmp_path / "later")})
    assert build.native_settings(ctx, {}, [])["SDK_HOME"][0] == str(tmp_path / "later")


def test_an_sdk_version_must_be_installed_under_the_sdk_directory(build, repo, machine_dirs):
    env_local(repo, {"WINDOWSSDKDIR": machine_dirs["WINDOWSSDKDIR"], "WINDOWSSDKVERSION": "10.0.9.0"})
    with pytest.raises(build.Problem, match=r"WINDOWSSDKVERSION \(\.env\.local\): 10\.0\.9\.0 is not installed under .* \(WINDOWSSDKDIR, \.env\.local\)"):
        build.native_settings(build.Ctx(repo), {}, [])
    env_local(repo, {"WINDOWSSDKDIR": machine_dirs["WINDOWSSDKDIR"], "WINDOWSSDKVERSION": "10.0.1.0\\"})
    assert "WINDOWSSDKVERSION" in build.native_settings(build.Ctx(repo), {}, []), "vcvars writes a trailing backslash"


def test_the_environment_is_checked_too(build, repo, tmp_path):
    with pytest.raises(build.Problem, match=r"LIBCLANG_PATH \(environment\)"):
        build.native_settings(build.Ctx(repo), {"LIBCLANG_PATH": str(tmp_path / "nowhere")}, [])


def test_llvm_dir_comes_first_on_path_and_gives_libclang_its_default(build, machine_dirs):
    native = {"LLVM_DIR": (machine_dirs["LLVM_DIR"], "x")}
    variables, directories = build.native_environment(native)
    assert directories == [str(Path(machine_dirs["LLVM_DIR"]) / "bin")]
    assert variables["LIBCLANG_PATH"] == str(Path(machine_dirs["LLVM_DIR"]) / "bin") and variables["LLVM_DIR"] == machine_dirs["LLVM_DIR"]
    given = {**native, "LIBCLANG_PATH": ("/elsewhere", "x")}
    assert build.native_environment(given)[0]["LIBCLANG_PATH"] == "/elsewhere"
    assert "SDK_HOME" not in build.native_environment({"SDK_HOME": ("/s", "x")})[0]


# --- release build -----------------------------------------------------------------------------------------------------


def test_release_build_hands_cargo_the_native_variables_and_puts_llvm_first(build, repo, machine_dirs, monkeypatch, capsys):
    stub = Stub(build, repo, monkeypatch)
    env_local(repo, machine_dirs)
    assert build.main(["--root", str(repo), "release", "build", "--target", "x86_64-pc-windows-msvc"]) == 0
    seen = stub.envs[0]
    for key, value in machine_dirs.items():
        assert seen[key] == value, key
    assert seen["PATH"].split(os.pathsep)[0] == str(Path(machine_dirs["LLVM_DIR"]) / "bin")
    out = capsys.readouterr().out
    assert f"native: VSINSTALLDIR={machine_dirs['VSINSTALLDIR']} (.env.local)" in out


def test_release_build_with_a_wrong_path_fails_before_cargo_runs_and_names_the_key(build, repo, machine_dirs, tmp_path, monkeypatch, capsys):
    stub = Stub(build, repo, monkeypatch)
    env_local(repo, {**machine_dirs, "WINDOWSSDKDIR": str(tmp_path / "nowhere"), "WINDOWSSDKVERSION": "10.0.1.0"})
    assert build.main(["--root", str(repo), "release", "build", "--target", "x86_64-pc-windows-msvc"]) == 1
    err = capsys.readouterr().err
    assert "WINDOWSSDKDIR (.env.local)" in err and "is not a directory" in err
    assert stub.commands == []


def test_release_build_with_the_flag_and_without_any_key_changes_nothing(build, repo, machine_dirs, monkeypatch, capsys):
    stub = Stub(build, repo, monkeypatch)
    assert build.main(["--root", str(repo), "release", "build", "--target", "x86_64-pc-windows-msvc"]) == 0
    assert "native:" not in capsys.readouterr().out
    assert build.main(["--root", str(repo), "release", "build", "--target", "x86_64-pc-windows-msvc", "--env", f"LIBCLANG_PATH={machine_dirs['LIBCLANG_PATH']}"]) == 0
    assert stub.envs[-1]["LIBCLANG_PATH"] == machine_dirs["LIBCLANG_PATH"]


# --- sdk install ------------------------------------------------------------------------------------------------------


def windows_repo(build, tmp_path) -> Path:
    root = init_repo(tmp_path / "winrepo")
    ctx_for(build, root, '[sdk]\ngroups = ["core"]\ninstall = ["core"]\n')
    rows = good_rows() + [
        "core llvm 22.1.8 windows-arm64 " + "a" * 64 + " https://example.test/llvm.tar.xz",
        "system Microsoft.VisualStudio.Component.VC.Tools.x86.x64 core windows-x64 - -",
        "system windows-sdk core windows-x64 - -",
    ]
    (root / "tools" / "sdk.lock").write_bytes(lock_text(*rows).encode())
    return root


def installer(build, root, native, plat="windows-x64"):
    ctx = build.Ctx(root)
    items = build.parse_lock_items((root / "tools" / "sdk.lock").read_text(encoding="utf-8"))
    return build.Installer(ctx, items, plat, root / "sdk", ["core", "system"], True, native)


def test_the_visual_studio_and_sdk_checks_use_the_named_directories_instead_of_vswhere(build, tmp_path, monkeypatch):
    root = windows_repo(build, tmp_path)
    # No Program Files here: vswhere and the default Windows Kits location are both absent.
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-program-files"))
    component = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
    assert not installer(build, root, {}).system_found(component)
    assert not installer(build, root, {}).system_found("windows-sdk")
    named = {"VSINSTALLDIR": ("/vs", ".env.local"), "WINDOWSSDKDIR": ("/kit", ".env.local")}
    assert installer(build, root, named).system_found(component)
    assert installer(build, root, named).system_found("windows-sdk")


def test_a_named_llvm_means_no_llvm_is_fetched(build, tmp_path, monkeypatch):
    root = windows_repo(build, tmp_path)
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "no-program-files"))
    assert installer(build, root, {}, "windows-arm64").clang_dir() is not None
    assert installer(build, root, {"LLVM_DIR": ("/llvm", ".env")}, "windows-arm64").clang_dir() is None


def test_sdk_home_chooses_the_prefix_after_the_flag_and_before_the_environment(build, tmp_path, machine, monkeypatch, capsys):
    root = windows_repo(build, tmp_path)
    home, flag, envhome = tmp_path / "home", tmp_path / "flag", tmp_path / "envhome"
    env_local(root, {"SDK_HOME": str(home)})
    monkeypatch.setenv("BUILD_SDK_HOME", str(envhome))
    base = ["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--groups", "core"]
    assert build.main(base) == 0
    assert (home / "env.sh").is_file() and not envhome.exists()
    assert f"{home} (SDK_HOME, .env.local)" in capsys.readouterr().out
    assert build.main([*base, "--prefix", str(flag)]) == 0
    assert (flag / "env.sh").is_file()


def test_the_env_files_the_install_writes_carry_the_native_variables(build, tmp_path, machine, machine_dirs, capsys):
    root = windows_repo(build, tmp_path)
    env_local(root, {**machine_dirs, "SDK_HOME": str(tmp_path / "sdkhome")})
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--groups", "core"]) == 0
    text = (tmp_path / "sdkhome" / "env.sh").read_text(encoding="utf-8")
    for key in ("VSINSTALLDIR", "VCTOOLSINSTALLDIR", "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "LIBCLANG_PATH", "LLVM_DIR"):
        assert f"export {key}='{machine_dirs[key]}'" in text, key
    assert str(Path(machine_dirs["LLVM_DIR"]) / "bin") in text.split("export PATH=")[1]
    ps1 = (tmp_path / "sdkhome" / "env.ps1").read_text(encoding="utf-8")
    assert f"$env:LIBCLANG_PATH = '{machine_dirs['LIBCLANG_PATH']}'" in ps1


def test_sdk_install_with_a_wrong_path_names_the_key_and_installs_nothing(build, tmp_path, machine, machine_dirs, capsys):
    root = windows_repo(build, tmp_path)
    env_local(root, {"VSINSTALLDIR": str(tmp_path / "nowhere"), "SDK_HOME": str(tmp_path / "sdkhome")})
    assert build.main(["--root", str(root), "sdk", "install", "--platform", "linux-x64", "--groups", "core"]) == 1
    assert "VSINSTALLDIR (.env.local)" in capsys.readouterr().err
    assert not (tmp_path / "sdkhome").exists() and machine.commands == []


# --- the container ignores them ---------------------------------------------------------------------------------------


def test_the_container_build_reports_a_native_key_as_ignored_and_not_as_a_non_default(build, tmp_path, machine_dirs, capsys):
    root = init_repo(tmp_path / "c")
    ctx_for(build, root, '[release]\nname = "t"\n[release.image]\nfile = "packaging/container/Containerfile"\n')
    (root / "Cargo.toml").write_text('[package]\nname = "t"\nversion = "1.0.0"\n', encoding="utf-8")
    (root / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    env_local(root, {"VSINSTALLDIR": machine_dirs["VSINSTALLDIR"]})
    args = type("A", (), {"env": ["DEPS_DIR=" + str(make(tmp_path / "d", "crates/"))], "tag": None})()
    plan = build.container_plan(build.Ctx(root), args, "", {}, fetch=False)
    assert plan.non_default and all(key != "VSINSTALLDIR" for key, _, _ in plan.non_default)
    assert any(note.startswith("VSINSTALLDIR (.env.local) is for native builds") for note in plan.notes)


def test_msvc_splat_dir_is_a_read_only_build_context_and_never_a_build_argument(build, tmp_path):
    root = init_repo(tmp_path / "c")
    ctx_for(build, root, '[release]\nname = "t"\n[release.image]\nfile = "packaging/container/Containerfile"\n')
    (root / "Cargo.toml").write_text('[package]\nname = "t"\nversion = "1.0.0"\n', encoding="utf-8")
    (root / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    splat = make(tmp_path / "splat", "crt/", "sdk/")
    deps = make(tmp_path / "d", "crates/")
    args = type("A", (), {"env": [f"DEPS_DIR={deps}", f"MSVC_SPLAT_DIR={splat}"], "tag": None})()
    plan = build.container_plan(build.Ctx(root), args, "cross-out", {}, fetch=False)
    contexts = [plan.command[i + 1] for i, word in enumerate(plan.command) if word == "--build-context"]
    assert f"msvc={splat.resolve()}" in contexts
    assert not [w for i, w in enumerate(plan.command) if plan.command[i - 1] == "--build-arg"]
    assert "build.py.non-default=DEPS_DIR,MSVC_SPLAT_DIR" in plan.command, "the label names the key, never the path"
    assert ("MSVC_SPLAT_DIR", str(splat), "--env") in plan.non_default
    bad = type("A", (), {"env": [f"DEPS_DIR={deps}", f"MSVC_SPLAT_DIR={make(tmp_path / 'bare')}"], "tag": None})()
    with pytest.raises(build.Problem, match=r"MSVC_SPLAT_DIR \(--env\): .* holds no crt, no sdk"):
        build.container_plan(build.Ctx(root), bad, "cross-out", {}, fetch=False)


# --- this machine's own Visual Studio, Windows SDK and LLVM ------------------------------------------------------------


def real_paths() -> dict[str, str] | None:
    if sys.platform != "win32":
        return None
    programs = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    vswhere = programs / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    found = subprocess.run([str(vswhere), "-products", "*", "-prerelease", "-property", "installationPath"], capture_output=True, text=True).stdout.split("\n")[0].strip()
    toolsets = sorted((Path(found) / "VC" / "Tools" / "MSVC").glob("*")) if found else []
    kit = programs / "Windows Kits" / "10"
    versions = sorted(p.name for p in (kit / "Lib").glob("10.*")) if (kit / "Lib").is_dir() else []
    llvm = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "LLVM"
    if not (found and toolsets and versions and (llvm / "bin" / "libclang.dll").is_file()):
        return None
    return {
        "VSINSTALLDIR": found,
        "VCTOOLSINSTALLDIR": str(toolsets[-1]),
        "WINDOWSSDKDIR": str(kit),
        "WINDOWSSDKVERSION": versions[-1],
        "LIBCLANG_PATH": str(llvm / "bin"),
        "LLVM_DIR": str(llvm),
    }


@pytest.mark.skipif(real_paths() is None, reason="no Visual Studio, Windows SDK and LLVM on this machine")
def test_the_real_paths_of_this_machine_pass_and_a_wrong_one_among_them_fails_by_name(build, repo, tmp_path):
    real = real_paths()
    env_local(repo, real)
    ctx = build.Ctx(repo)
    got = build.native_settings(ctx, {}, [])
    assert {k: v[0] for k, v in got.items()} == real
    variables, directories = build.native_environment(got)
    assert variables["LIBCLANG_PATH"] == real["LIBCLANG_PATH"] and directories == [str(Path(real["LLVM_DIR"]) / "bin")]
    for key in ("VSINSTALLDIR", "WINDOWSSDKDIR", "LIBCLANG_PATH"):
        env_local(repo, {**real, key: str(tmp_path / "wrong")})
        with pytest.raises(build.Problem, match=rf"^{key} \(\.env\.local\): "):
            build.native_settings(ctx, {}, [])
