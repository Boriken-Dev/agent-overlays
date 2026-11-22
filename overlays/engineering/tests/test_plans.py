"""`plans`: status-folder layout, YAML frontmatter, complex `<name>/README.md`, SQL query, no INDEX.md."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("duho")

OVERLAY = Path(__file__).resolve().parents[1]
_LIB = OVERLAY / "lib" / "python"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

MODULE = OVERLAY / "skills" / "plans" / "plans.py"
_spec = importlib.util.spec_from_file_location("plans_model", _LIB / "_plans.py")
plans = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plans
_spec.loader.exec_module(plans)

PHASES = ["parse the header", "encode the options", "wire the relay"]


@pytest.fixture
def store(tmp_path):
    return plans.PlansStore(tmp_path / "plans")


@pytest.fixture
def plan(store):
    made = store.add("relay_support", title="Relay support",
                     executor="executor/code — routine", phases=PHASES)
    store.set_status("relay_support", "ready")
    return made


def reload(store, name="relay_support"):
    return store.require(name)


def test_add_writes_frontmatter_in_draft_folder_and_no_index(store):
    made = store.add("Relay Support", title="Relay support", phases=PHASES, attrs={"priority": "high"})
    assert made.path == store.root / "draft" / "relay_support.md"
    assert not (store.root / "INDEX.md").exists()
    raw = made.path.read_bytes().decode("utf-8")
    assert "\r" not in raw
    assert raw.startswith("---\n")
    # The folder says the status, the file name the name and the heading the title: the frontmatter repeats none.
    head = raw.split("---\n")[1]
    assert "status:" not in head and "name:" not in head and "title:" not in head
    assert made.status == "draft"
    assert "priority: high" in raw
    assert "# Relay support\n" in raw
    assert "<!--" not in raw
    assert [it.name for it in made.items()] == PHASES
    assert "### Phase 3: wire the relay" in raw


def test_add_refuses_a_second_plan_of_the_same_name(store, plan):
    with pytest.raises(SystemExit, match="already exists"):
        store.add("relay_support")


def test_complex_plan_uses_readme_and_moves_whole_directory(store):
    made = store.add("big_epic", title="Big epic", executor="architecture-planner/plan — multi",
                     phases=["one", "two"], complex_dir=True)
    assert made.path == store.root / "draft" / "big_epic" / "README.md"
    extra = made.path.parent / "notes.md"
    extra.write_text("# Extra context\n", encoding="utf-8")

    ready = store.set_status("big_epic", "ready")
    assert ready.path == store.root / "ready" / "big_epic" / "README.md"
    assert (store.root / "ready" / "big_epic" / "notes.md").is_file()
    assert not (store.root / "draft" / "big_epic").exists()
    assert len(store.require("big_epic").extras) == 1


def test_adding_subplan_promotes_single_file_to_readme_dir(store, plan):
    sub = store.add("relay_support/option_82", title="Option 82")
    parent = store.require("relay_support")
    assert parent.path == store.root / "ready" / "relay_support" / "README.md"
    assert sub.path == store.root / "ready" / "relay_support" / "option_82.md"


def test_a_step_changes_the_progress_line_and_updated(store, plan):
    loaded = reload(store)
    loaded.mark("2", "/", None)
    store.save(loaded)
    reloaded = reload(store)
    assert reloaded.item("2").state == "active"
    assert reloaded.meta.get("updated")


def test_check_keeps_the_outcome_on_the_line(store, plan):
    loaded = reload(store)
    loaded.mark("1", "x", "24 tests   pass")
    store.save(loaded)
    item = reload(store).item("1")
    assert (item.state, item.note) == ("done", "24 tests pass")
    assert reload(store).counts() == (1, 3)


def test_uncheck_drops_the_note(store, plan):
    loaded = reload(store)
    loaded.mark("1", "!", "no compiler")
    loaded.mark("1", " ", "")
    assert loaded.item("1").render() == "- [ ] Phase 1: parse the header"


def test_an_unknown_phase_names_the_ones_that_exist(store, plan):
    with pytest.raises(SystemExit, match="it has: 1, 2, 3"):
        reload(store).item("9")


def test_current_is_the_active_phase_then_the_first_pending(store, plan):
    loaded = reload(store)
    assert loaded.current().id == "1"
    loaded.mark("1", "x", "done")
    loaded.mark("3", "/", None)
    assert loaded.current().id == "3"
    loaded.mark("3", "x", "done")
    loaded.mark("2", "x", "done")
    assert loaded.current() is None


def test_one_phase_or_one_section_can_be_read_alone(store, plan):
    loaded = reload(store)
    block = loaded.phase_text("2")
    assert block.startswith("### Phase 2: encode the options\n")
    assert "Phase 3" not in block
    progress = loaded.section_text("Progress")
    assert progress.count("- [") == 3 and "## Phases" not in progress


def test_note_appends_to_known_facts(store, plan):
    loaded = reload(store)
    loaded.append_fact("the relay keeps the client's port")
    store.save(loaded)
    facts = reload(store).section_text("Known Facts & Context")
    assert facts.rstrip().endswith("- the relay keeps the client's port")


def test_done_needs_every_phase_checked_and_evidence(store, plan):
    with pytest.raises(SystemExit, match="not finished"):
        store.set_status("relay_support", "done", note="shipped")
    loaded = reload(store)
    for phase in ("1", "2", "3"):
        loaded.mark(phase, "x", "ok")
    store.save(loaded)
    with pytest.raises(SystemExit, match="needs -m"):
        store.set_status("relay_support", "done")


def test_done_moves_the_plan_and_its_sub_plans_to_completed(store, plan):
    store.add("relay_support/option_82", title="Option 82")
    loaded = reload(store)
    for phase in ("1", "2", "3"):
        loaded.mark(phase, "x", "ok")
    store.save(loaded)
    closed = store.set_status("relay_support", "done", note="suite green on both platforms")
    assert closed.path == store.root / "completed" / "relay_support" / "README.md"
    assert not (store.root / "ready" / "relay_support").exists()
    assert (store.root / "completed" / "relay_support" / "option_82.md").is_file()
    assert closed.field("outcome") == "suite green on both platforms"
    assert closed.field("closed")
    assert [p.name for p in store.live()] == []


def test_on_hold_needs_a_reason_and_a_live_status_brings_it_back(store, plan):
    with pytest.raises(SystemExit, match="needs -m"):
        store.set_status("relay_support", "on-hold")
    parked = store.set_status("relay_support", "on-hold", note="waiting for the dependency")
    assert parked.path == store.root / "on-hold" / "relay_support.md"
    back = store.set_status("relay_support", "ready")
    assert back.path == store.root / "ready" / "relay_support.md"
    assert not back.field("held")


def test_sql_query_set_edit_and_migrate_legacy(store):
    # Legacy flat plan + legacy INDEX.md
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "INDEX.md").write_text("# Plans\n", encoding="utf-8")
    (store.root / "legacy_plan.md").write_text(
        "# Legacy plan\n\nStatus: ready\nExecutor: code-executor/implemented\n\n"
        "## Progress\n\n- [ ] Phase 1: fix\n\n## Known Facts & Context\n\n- fact\n\n"
        "## Phases\n\n### Phase 1: fix\n\n- Files: a.py\n- Guidance: g\n- Why: w\n- Done-when: d\n\n"
        "## Verification\n\n- pytest -> ok\n\n## Reporting\n\n- h\n",
        encoding="utf-8",
    )
    res = store.migrate()
    assert res["removed_index"] is True
    assert not (store.root / "INDEX.md").exists()
    migrated = store.require("legacy_plan")
    assert migrated.path == store.root / "ready" / "legacy_plan.md"
    assert migrated.status == "ready"
    assert "status" not in migrated.meta
    assert "status:" not in migrated.path.read_text(encoding="utf-8").split("---\n")[1]
    assert migrated.meta["executor"] == "code-executor/implemented"

    store.set_attrs("legacy_plan", {"priority": "high"})
    cols, rows = store.query(sql="SELECT name, status, priority FROM plans WHERE priority = 'high'")
    assert rows == [{"name": "legacy_plan", "status": "ready", "priority": "high"}]

    store.edit_body("legacy_plan", section="facts", append="- learned something")
    assert "- learned something" in store.require("legacy_plan").section_text("Known Facts & Context")


def test_review_reports_are_not_plans(store, plan):
    report = store.root / "ready" / "relay_support" / "reviews" / "reviewer.md"
    report.parent.mkdir(parents=True)
    report.write_text("## R1 — findings\n", encoding="utf-8")
    assert [p.name for p in store.all()] == ["relay_support"]


def test_validate_reports_what_is_missing(store):
    made = store.add("fresh")
    problems = "\n".join(made.problems())
    assert "unfilled placeholders" in problems
    assert "no `### Phase 2:` block" in problems

    good = store.add("filled", title="Filled", executor="executor/code — routine",
                     phases=["one"])
    text = good.path.read_text(encoding="utf-8")
    for placeholder, value in (("<paths>", "src/x.py"), ("<signatures, hints, defaults>", "f(x)"),
                               ("<one line>", "needed"), ("<observable check>", "tests pass"),
                               ("<fact>", "measured"), ("<command> → <expected>", "pytest → green")):
        text = text.replace(placeholder, value)
    with open(good.path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    assert store.require("filled").problems() == []

    text += ("\n- `src/<pkg>/AGENTS.md` and `Box<dyn K>` are quoted.\n"
             "```rust\nfn f() -> Vec<u8> {}\n```\n")
    with open(good.path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    assert store.require("filled").problems() == []
    with open(good.path, "a", encoding="utf-8", newline="") as handle:
        handle.write("- left: <todo>\n")
    assert "unfilled placeholders: <todo>" in "\n".join(store.require("filled").problems())


def run_cli(tmp_path, *argv):
    env = dict(os.environ, AGENTS_HOME=str(tmp_path / "store"), HOME=str(tmp_path),
               USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    env.pop("AGENTS_PROJECT_ROOT", None)
    return subprocess.run(
        [sys.executable, str(MODULE), *argv],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))


def test_the_command_line_drives_a_plan_from_draft_to_done(tmp_path):
    d = str(tmp_path / "plans")
    out = run_cli(tmp_path, "add", "relay_support", "--dir", d, "-t", "Relay support",
                  "-p", "parse", "-p", "encode")
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "plans" / "draft" / "relay_support.md").is_file()
    assert not (tmp_path / "plans" / "INDEX.md").exists()
    assert run_cli(tmp_path, "status", "relay_support", "ready", "--dir", d).returncode == 0
    assert (tmp_path / "plans" / "ready" / "relay_support.md").is_file()

    started = run_cli(tmp_path, "start", "relay_support", "1", "--dir", d)
    assert started.returncode == 0, started.stderr
    assert "[/] Phase 1: parse" in started.stdout and "0/2 done" in started.stdout
    assert (tmp_path / "plans" / "executing" / "relay_support.md").is_file()
    assert "[executing]" in run_cli(tmp_path, "list", "--dir", d).stdout

    q = run_cli(tmp_path, "query", "--dir", d, "--where", "status = 'executing'", "--format", "json")
    assert q.returncode == 0, q.stderr
    assert json.loads(q.stdout)[0]["name"] == "relay_support"

    no_outcome = run_cli(tmp_path, "check", "relay_support", "1", "--dir", d)
    assert no_outcome.returncode != 0 and "pass -m" in no_outcome.stderr

    checked = run_cli(tmp_path, "check", "relay_support", "1", "--dir", d, "-m", "12 tests pass")
    assert "1/2 done" in checked.stdout

    nxt = run_cli(tmp_path, "next", "relay_support", "--dir", d)
    assert "### Phase 2: encode" in nxt.stdout and "### Phase 1" not in nxt.stdout

    run_cli(tmp_path, "check", "relay_support", "2", "--dir", d, "-m", "round trip holds")
    closed = run_cli(tmp_path, "status", "relay_support", "done", "--dir", d, "-m", "suite green")
    assert closed.returncode == 0, closed.stderr
    assert (tmp_path / "plans" / "completed" / "relay_support.md").is_file()
    assert run_cli(tmp_path, "list", "--dir", d).stdout.strip() == ""
