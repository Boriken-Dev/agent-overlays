"""`gate` affected-package selection: which workspace packages a branch reaches, `slow` filters, `--full`.

The workspace is a hand-built `cargo metadata` document over a temporary git repository, so no cargo runs."""
from __future__ import annotations

import pytest

from tools_env import no_machine_agent_files  # noqa: F401  (an autouse fixture)

import _affected as aff
import _gate
from test_gate import duho, gate_cmd, git, repo, run, wsl_config, write_project  # noqa: F401  (repo is a fixture)

TEST = "cargo test --workspace --all-features --no-fail-fast"


def metadata(top):
    """core <- app <- tool, and an unrelated leaf; every package lives under crates/."""
    def package(name, deps=()):
        return {"name": name, "manifest_path": str(top / "crates" / name / "Cargo.toml"),
                "dependencies": [{"name": d} for d in deps] + [{"name": "serde"}]}
    return {"workspace_root": str(top),
            "packages": [package("core"), package("app", ["core"]), package("tool", ["app"]), package("leaf")]}


@pytest.fixture
def ws(repo, monkeypatch):
    """The repository holds the four crates; `origin/main` is the commit before they were touched."""
    for name in ("core", "app", "tool", "leaf"):
        (repo / "crates" / name / "src").mkdir(parents=True)
        (repo / "crates" / name / "src" / "lib.rs").write_text("\n", encoding="utf-8")
    (repo / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: crates")
    git(repo, "push", "-q", "origin", "main")
    monkeypatch.setattr(aff, "load_metadata", lambda cwd: metadata(repo))
    seen = []
    monkeypatch.setattr(_gate, "run_step", lambda step, *a, **k: seen.append(step) or True)
    return repo, seen


def change(repo, *paths):
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text(encoding="utf-8") + "x\n" if target.exists() else "x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: change")


def gate(repo, tmp_path, steps, **kw):
    write_project(repo, steps)
    return run(repo, tmp_path / "out", **kw)


def step(**extra):
    return {"name": "test", "run": TEST, "affected": True, **extra}


def packages_of(argv):
    return [argv[i + 1] for i, token in enumerate(argv) if token == "-p"]


def test_a_changed_leaf_runs_only_itself(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    ok, lines = gate(repo, tmp_path, [step()])
    assert ok and packages_of(seen[0]["run"]) == ["leaf"]
    assert "--workspace" not in seen[0]["run"]
    assert seen[0]["run"][:5] == ["cargo", "test", "-p", "leaf", "--all-features"]
    assert "--no-fail-fast" in seen[0]["run"]
    assert lines[0] == "AFFECTED test: -p leaf"
    assert lines[1] == "  leaf <- crates/leaf/src/lib.rs"
    assert (tmp_path / "out" / "test.affected").read_text(encoding="utf-8").startswith("AFFECTED test: -p leaf")


def test_a_changed_library_runs_its_dependents_transitively(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/core/src/lib.rs")
    ok, lines = gate(repo, tmp_path, [step()])
    assert ok and packages_of(seen[0]["run"]) == ["core", "app", "tool"]
    assert "  app <- depends on core" in lines and "  tool <- depends on app" in lines
    assert "leaf" not in " ".join(lines)


def test_a_lock_file_change_runs_the_whole_workspace(ws, tmp_path):
    repo, seen = ws
    change(repo, "Cargo.lock")
    ok, lines = gate(repo, tmp_path, [step()])
    assert ok and seen[0]["run"] == TEST and lines[0] == "AFFECTED test: whole workspace (Cargo.lock changed)"


def test_a_path_named_by_full_when_runs_the_whole_workspace(ws, tmp_path):
    repo, seen = ws
    change(repo, "examples/config/a.toml")
    ok, lines = gate(repo, tmp_path, [step(full_when=["examples/**"])])
    assert seen[0]["run"] == TEST and "examples/config/a.toml changed" in lines[0]


def test_no_rust_change_skips_the_step_with_the_reason(ws, tmp_path):
    repo, seen = ws
    change(repo, "docs/d.md")
    ok, lines = gate(repo, tmp_path, [step()])
    assert ok and seen == []
    assert lines[0] == "AFFECTED test: no package affected"
    assert "SKIP test (no workspace package is affected)" in lines


def test_full_ignores_affected(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    ok, lines = gate(repo, tmp_path, [step(slow=[{"package": "core", "skip": "live_"}])], full=True)
    assert ok and seen[0]["run"] == TEST and not any(line.startswith("AFFECTED") for line in lines)


def test_without_a_remote_main_everything_runs(ws, tmp_path):
    repo, seen = ws
    git(repo, "remote", "remove", "origin")
    gate(repo, tmp_path, [step()])
    assert seen[0]["run"] == TEST


def test_a_step_without_affected_is_untouched(ws, tmp_path):
    repo, seen = ws
    change(repo, "docs/d.md")
    gate(repo, tmp_path, [{"name": "test", "run": TEST}])
    assert seen[0]["run"] == TEST


def test_slow_filters_are_skipped_unless_their_own_package_changed(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/core/src/lib.rs")
    slow = [{"package": "core", "skip": "live_"}, {"package": "tool", "skip": ["load_", "timing_"]}]
    ok, lines = gate(repo, tmp_path, [step(slow=slow)])
    # core changed itself: its live tests run. tool is only affected through app: its tests are skipped.
    assert seen[0]["run"][-5:] == ["--", "--skip", "load_", "--skip", "timing_"]
    assert "live_" not in seen[0]["run"]
    assert "  slow live_: run (core changed)" in lines and "  slow load_, timing_: skipped (tool unchanged)" in lines


def test_slow_goes_after_an_existing_double_dash(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    gate(repo, tmp_path, [step(run=TEST + " -- --test-threads=1", slow=[{"package": "core", "skip": "live_"}])])
    assert seen[0]["run"].count("--") == 1
    assert seen[0]["run"][-3:] == ["--test-threads=1", "--skip", "live_"]


def test_exclude_drops_the_package_from_the_set(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/core/src/lib.rs")
    gate(repo, tmp_path, [step(run=TEST + " --exclude tool")])
    assert packages_of(seen[0]["run"]) == ["core", "app"] and "--exclude" not in seen[0]["run"]


def test_a_step_that_names_its_own_package_is_refused(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    with pytest.raises(_gate.GateError, match="names no `-p` of its own"):
        gate(repo, tmp_path, [step(run="cargo test -p core")])


def test_a_step_that_is_not_cargo_is_refused(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    with pytest.raises(_gate.GateError, match="needs a step that runs cargo"):
        gate(repo, tmp_path, [step(run="make test")])


def test_slow_naming_an_unknown_package_is_refused(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    with pytest.raises(_gate.GateError, match="not a workspace package"):
        gate(repo, tmp_path, [step(slow=[{"package": "nope", "skip": "x"}])])


def test_the_linux_leg_skips_when_nothing_is_affected(ws, tmp_path):
    repo, seen = ws
    change(repo, "docs/d.md")
    steps = [{"name": "test", "run": TEST, "affected": True}]
    wsl_config(tmp_path, steps)
    write_project(repo, [{"name": "x", "run": "true"}], wsl_steps=steps)
    lines = []
    assert _gate.run_wsl(repo, out=tmp_path / "out", say=lines.append, lock_every=0.05)
    assert "SKIP wsl:test (no workspace package is affected)" in lines


def test_the_linux_leg_script_runs_only_the_affected_packages(ws, tmp_path):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    out = tmp_path / "out"
    out.mkdir()
    planned = _gate.plan_affected({"name": "test", "run": TEST, "affected": True}, repo, ["crates/leaf/src/lib.rs"],
                                  full=False, cache={}, out=out, say=lambda line: None, label="wsl:test")
    assert planned["run"][:4] == ["cargo", "test", "-p", "leaf"]


def test_full_goes_through_the_command_line(ws, tmp_path, capsys):
    repo, seen = ws
    change(repo, "crates/leaf/src/lib.rs")
    write_project(repo, [step()])
    for flags, wanted in (([], ["-p", "leaf"]), (["--full"], ["--workspace"])):
        seen.clear()
        command = duho.parse(gate_cmd.Gate.Run, [str(repo), "--out", str(tmp_path / "out"), "--lock-every", "0.05",
                                                *flags])
        assert command() == 0
        assert all(w in seen[0]["run"] for w in wanted)


def test_affected_prints_the_arguments_for_a_script(ws, tmp_path, capsys):
    repo, seen = ws
    change(repo, "crates/core/src/lib.rs")
    write_project(repo, [step(slow=[{"package": "tool", "skip": "load_"}])])
    command = duho.parse(gate_cmd.Gate.Affected, [str(repo), "--args", "--step", "test"])
    assert command() == 0
    assert capsys.readouterr().out.strip() == "-p core -p app -p tool -- --skip load_"
    change(repo, "Cargo.lock")
    assert duho.parse(gate_cmd.Gate.Affected, [str(repo), "--args"])() == 0
    assert capsys.readouterr().out.strip() == "--workspace"


def test_affected_prints_nothing_when_no_package_is_reached(ws, tmp_path, capsys):
    repo, seen = ws
    change(repo, "docs/d.md")
    write_project(repo, [step()])
    assert duho.parse(gate_cmd.Gate.Affected, [str(repo), "--args"])() == 0
    assert capsys.readouterr().out == ""


def test_the_root_package_owns_only_its_source_directories():
    packages = [aff.Package("root", ""), aff.Package("sub", "crates/sub")]
    selection = aff.select(packages, ["README.md", "src/lib.rs", "crates/sub/src/x.rs"], lambda path: False)
    assert selection.packages == ["root", "sub"] and selection.outside == ["README.md"]
