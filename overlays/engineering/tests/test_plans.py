"""`plans`: status-folder layout, YAML frontmatter, complex `<name>/README.md`, SQL query, no INDEX.md."""
from __future__ import annotations

import importlib.util
import json
import os
import re
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
        handle.write("- <todo>\n")
    assert "unfilled placeholders: <todo>" in "\n".join(store.require("filled").problems())


def test_only_template_placeholders_count_as_unfilled(store):
    made = store.add("prose", title="Prose", executor="executor/code — routine", phases=["one"])
    text = made.path.read_text(encoding="utf-8")
    for placeholder, value in (("<paths>", "src/x.py"), ("<one line>", "needed"), ("<observable check>", "ok"),
                               ("<fact>", "measured"), ("<command> → <expected>", "pytest → green")):
        text = text.replace(placeholder, value)
    text = re.sub(r"<signatures, hints, defaults[^<>\n]*>", "f(x)", text)
    text += "\n- The `<id>` and `<scope>` keys, and a plain <id> or <scope> in prose, are not placeholders.\n"
    with open(made.path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    assert store.require("prose").problems() == []
    with open(made.path, "a", encoding="utf-8", newline="") as handle:
        handle.write("- Files: <paths>\n")
    assert "unfilled placeholders: <paths>" in "\n".join(store.require("prose").problems())


def test_validate_with_no_name_checks_every_plan(tmp_path):
    d = str(tmp_path / "plans")
    assert run_cli(tmp_path, "add", "fresh", "--dir", d, "-t", "Fresh").returncode == 0
    out = run_cli(tmp_path, "validate", "--dir", d)
    assert out.returncode == 1, out.stderr
    assert "fresh: unfilled placeholders" in out.stdout and "1 plans, 1 with problems" in out.stdout
    other = tmp_path / "other"
    other.mkdir()
    empty = run_cli(other, "validate", "--dir", str(other / "plans"))
    assert empty.returncode == 0 and "0 plans, 0 with problems" in empty.stdout


def test_migrate_leaves_a_plan_already_in_completed_alone(store):
    done = store.root / "completed"
    done.mkdir(parents=True)
    old = "---\nname: old_one\nstatus: done\ncreated: 2026-01-01\n---\n\n# Old\n\nSee <id>.\r\n"
    with open(done / "old_one.md", "w", encoding="utf-8", newline="") as handle:
        handle.write(old)
    res = store.migrate()
    assert res["moved"] == [] and res["updated"] == []
    assert (done / "old_one.md").read_bytes() == old.encode("utf-8")


def run_cli(tmp_path, *argv):
    env = dict(os.environ, AGENTS_HOME=str(tmp_path / "store"), HOME=str(tmp_path),
               USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8",
               ENGINEERING_OVERLAY_ROOT=str(OVERLAY))
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


# -- editing and reading parts ------------------------------------------------

def body_of(store):
    return reload(store).path.read_text(encoding="utf-8")


def test_set_replaces_only_its_section(store, plan):
    store.edit_body("relay_support", section="facts", set_text="- one fact\n- another")
    after = reload(store)
    assert after.section_text("Known Facts & Context").splitlines()[-2:] == ["- one fact", "- another"]
    assert "<fact>" not in after.section_text("Known Facts & Context")
    for kept in ("## Progress", "### Phase 2: encode the options", "## Verification", "# Relay support"):
        assert kept in body_of(store)
    assert [it.name for it in after.items()] == PHASES


def test_a_body_is_refused_with_a_section_and_without_whole(store, plan):
    with pytest.raises(SystemExit, match="--section facts --set"):
        store.edit_body("relay_support", section="facts", body="x", whole=True)
    with pytest.raises(SystemExit, match="--whole"):
        store.edit_body("relay_support", body="x")
    assert "### Phase 3" in body_of(store)
    store.edit_body("relay_support", body="# Fresh\n", whole=True)
    assert "### Phase 3" not in body_of(store)


def test_an_unknown_section_lists_the_sections(store, plan):
    for kwargs in ({"set_text": "x"}, {"append": "x"}, {"replace": ("a", "b")}):
        with pytest.raises(SystemExit, match=r"no section 'nope'.*facts \(Known Facts & Context\)"):
            store.edit_body("relay_support", section="nope", **kwargs)
    with pytest.raises(SystemExit, match="--section"):
        store.edit_body("relay_support", set_text="x")


def test_a_section_text_cannot_start_another_section(store, plan):
    with pytest.raises(SystemExit, match="new section"):
        store.edit_body("relay_support", section="facts", set_text="- a\n## Oops\n")
    store.edit_body("relay_support", section="facts", set_text="```\n## in a fence\n```")
    assert "## in a fence" in body_of(store)


def test_append_and_replace_stay_inside_the_section(store, plan):
    store.edit_body("relay_support", section="verification", append="- second check")
    assert reload(store).section_text("Verification").rstrip().endswith("- second check")
    with pytest.raises(SystemExit, match="not found in section 'Verification'"):
        store.edit_body("relay_support", section="verification", replace=("Phase 1", "x"))
    store.edit_body("relay_support", section="phases", replace=("wire the relay", "wire it"))
    assert "### Phase 3: wire it" in body_of(store)


def test_phase_view_is_the_progress_line_then_the_block(store, plan):
    loaded = reload(store)
    loaded.mark("2", "x", "done")
    view = loaded.phase_view("2")
    assert view.startswith("- [x] Phase 2: encode the options — done\n\n### Phase 2: encode")
    assert "Phase 3" not in view


def test_a_note_goes_under_its_phase_block(store, plan):
    store.note_phase("relay_support", "2", "the options field is 16 bits")
    text = body_of(store)
    assert reload(store).phase_text("2").rstrip().endswith("- Note: the options field is 16 bits")
    assert text.index("- Note:") < text.index("### Phase 3")
    assert "- Note:" not in reload(store).section_text("Known Facts & Context")
    with pytest.raises(SystemExit, match="no phase"):
        store.note_phase("relay_support", "9", "x")


def test_phase_add_inserts_both_places_and_renumbers(store, plan):
    _, number = store.add_phase("relay_support", "review the wire format", after="1")
    loaded = reload(store)
    assert number == "2"
    assert [(it.id, it.name) for it in loaded.items()] == [
        ("1", PHASES[0]), ("2", "review the wire format"), ("3", PHASES[1]), ("4", PHASES[2])]
    assert "### Phase 4: wire the relay" in body_of(store)
    assert loaded.phase_text("3").startswith("### Phase 3: encode the options")
    store.add_phase("relay_support", "last one")
    store.add_phase("relay_support", "first one", after="0")
    names = [it.name for it in reload(store).items()]
    assert names[0] == "first one" and names[-1] == "last one" and len(names) == 6
    assert reload(store).phase_text("1").startswith("### Phase 1: first one")


def test_phase_add_keeps_boxes_and_notes_with_their_phase(store, plan):
    loaded = reload(store)
    loaded.mark("1", "x", "shipped")
    store.save(loaded)
    store.add_phase("relay_support", "new first", after="0")
    items = reload(store).items()
    assert (items[1].id, items[1].box, items[1].note) == ("2", "x", "shipped")


def test_phase_rename_changes_the_line_and_the_heading(store, plan):
    store.rename_phase("relay_support", "2", "encode the flags")
    loaded = reload(store)
    assert loaded.item("2").name == "encode the flags"
    assert loaded.phase_text("2").startswith("### Phase 2: encode the flags")


def test_phase_remove_renumbers_and_refuses_a_checked_phase(store, plan):
    loaded = reload(store)
    loaded.mark("1", "x", "done")
    store.save(loaded)
    with pytest.raises(SystemExit, match="--force"):
        store.remove_phase("relay_support", "1")
    store.remove_phase("relay_support", "2")
    loaded = reload(store)
    assert [(it.id, it.name) for it in loaded.items()] == [("1", PHASES[0]), ("2", PHASES[2])]
    assert loaded.phase_text("2").startswith("### Phase 2: wire the relay")
    assert "encode the options" not in body_of(store)
    store.remove_phase("relay_support", "1", force=True)
    with pytest.raises(SystemExit, match="only phase"):
        store.remove_phase("relay_support", "1", force=True)


def test_phase_split_makes_template_blocks_for_the_new_parts(store, plan):
    store.split_phase("relay_support", "2", ["encode flags", "encode lengths", "encode tail"])
    loaded = reload(store)
    assert [(it.id, it.name) for it in loaded.items()] == [
        ("1", PHASES[0]), ("2", "encode flags"), ("3", "encode lengths"), ("4", "encode tail"),
        ("5", PHASES[2])]
    assert loaded.phase_text("3").startswith("### Phase 3: encode lengths")
    assert "<paths>" in loaded.phase_text("3")
    with pytest.raises(SystemExit, match="at least two"):
        store.split_phase("relay_support", "1", ["only one"])


def test_phase_management_refuses_a_plan_whose_blocks_disagree(store, plan):
    store.edit_body("relay_support", section="phases", replace=("### Phase 3:", "### Phase 7:"))
    with pytest.raises(SystemExit, match="disagree"):
        store.add_phase("relay_support", "x")


def test_the_command_line_reads_and_edits_parts(tmp_path):
    d = str(tmp_path / "plans")

    def run(*a):
        return run_cli(tmp_path, *a, "--dir", d)

    assert run("add", "relay_support", "-t", "Relay support", "-p", "parse", "-p", "encode").returncode == 0

    refused = run("edit", "relay_support", "--section", "facts", "--body", "x")
    assert refused.returncode != 0 and "--set" in refused.stderr
    assert "--whole" in run("edit", "relay_support", "--body", "x").stderr
    both = run("edit", "relay_support", "--section", "facts", "--set", "a", "--append", "b")
    assert both.returncode != 0 and "one edit" in both.stderr
    assert "facts" in run("edit", "relay_support", "--section", "nope", "--set", "x").stderr

    f = tmp_path / "facts.md"
    f.write_text("- from a file\n", encoding="utf-8")
    assert run("edit", "relay_support", "-s", "facts", "--set-file", str(f)).returncode == 0
    g = tmp_path / "more.md"
    g.write_text("- appended from a file\n", encoding="utf-8")
    assert run("edit", "relay_support", "-s", "facts", "--append-file", str(g)).returncode == 0
    facts = run("show", "relay_support", "--section", "facts").stdout
    assert "- from a file\n- appended from a file" in facts and "<fact>" not in facts
    assert "### Phase 2: encode" in run("show", "relay_support", "--section", "phases").stdout

    phase = run("show", "relay_support", "--phase", "2").stdout
    assert phase.startswith("- [ ] Phase 2: encode\n\n### Phase 2: encode") and "Phase 1" not in phase
    brief = run("show", "relay_support", "--brief").stdout
    assert "# Relay support" in brief and "- [ ] Phase 2: encode" in brief and "### Phase" not in brief

    assert run("phases", "relay_support").stdout.splitlines()[0].split() == ["1", "pending", "parse"]
    run("status", "relay_support", "ready")
    run("check", "relay_support", "1", "-m", "ok")
    assert [p["state"] for p in json.loads(run("phases", "relay_support", "--format", "json").stdout)] == [
        "done", "pending"]
    assert run("phases", "relay_support", "--open").stdout.split()[:3] == ["2", "pending", "encode"]
    assert run("next", "relay_support", "--block").stdout.startswith("### Phase 2: encode")

    assert run("phase-add", "relay_support", "review", "--after", "1").returncode == 0
    assert run("phase-rename", "relay_support", "2", "peer review").returncode == 0
    assert run("phase-split", "relay_support", "3", "encode a", "encode b").returncode == 0
    removed = run("phase-remove", "relay_support", "1")
    assert removed.returncode != 0 and "--force" in removed.stderr
    assert run("phase-remove", "relay_support", "4").returncode == 0
    names = [p["title"] for p in json.loads(run("phases", "relay_support", "-f", "json").stdout)]
    assert names == ["parse", "peer review", "encode a"]

    noted = run("note", "relay_support", "-m", "port is 4433", "--phase", "2")
    assert noted.returncode == 0 and "Phase 2" in noted.stdout
    assert "- Note: port is 4433" in run("show", "relay_support", "-p", "2").stdout
    assert run("note", "relay_support", "-m", "a general fact").returncode == 0
    assert "- a general fact" in run("show", "relay_support", "-s", "facts").stdout


def test_next_does_not_call_a_blocked_plan_finished(tmp_path):
    d = str(tmp_path / "plans")
    run_cli(tmp_path, "add", "relay_support", "--dir", d, "-t", "Relay", "-p", "parse", "-p", "verify")
    run_cli(tmp_path, "status", "relay_support", "ready", "--dir", d)
    run_cli(tmp_path, "start", "relay_support", "1", "--dir", d)
    run_cli(tmp_path, "check", "relay_support", "1", "--dir", d, "-m", "ok")
    blocked = run_cli(tmp_path, "block", "relay_support", "2", "--dir", d, "-m", "needs the VM")
    assert blocked.returncode == 0, blocked.stderr

    nxt = run_cli(tmp_path, "next", "relay_support", "--dir", d)
    assert nxt.returncode == 0, nxt.stderr
    assert "Every phase is checked" not in nxt.stdout
    assert "blocked" in nxt.stdout and "Phase 2: verify" in nxt.stdout and "needs the VM" in nxt.stdout

    only = run_cli(tmp_path, "next", "relay_support", "--dir", d, "--block")
    assert only.returncode != 0
    assert "every phase is checked" not in only.stderr and "blocked" in only.stderr
