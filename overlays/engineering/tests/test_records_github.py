"""The GitHub provider against a stand-in `gh` (tests/standin_gh.py): no network, no token."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

OVERLAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OVERLAY / "lib" / "python"))

import _records  # noqa: E402

STANDIN = (Path(__file__).resolve().parent / "standin_gh.py").as_posix()


@pytest.fixture
def gh(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setenv("STANDIN_STATE", str(state))
    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "records.toml").write_text(
        '[plans]\nprovider = "github"\nrepo = "o/r"\ncommand = ["%s", "%s"]\n'
        % (Path(sys.executable).as_posix(), STANDIN),
        encoding="utf-8",
    )
    store = _records.FolderStore(agents / "plans")
    prov = _records.open_provider(store, agents, scope_key="plans")
    prov.state = state
    return prov


def calls(prov):
    return [json.loads(l) for l in Path(str(prov.state) + ".calls").read_text(encoding="utf-8").splitlines()]


def test_create_get_and_query(gh):
    made = gh.create("Slow start", meta={"severity": "high", "tags": ["a", "b"]}, body="## Context\n\nIt hangs.\n")
    # The tracker names the record: its number is the key, the title is a field.
    assert made["name"] == "1" and made["title"] == "Slow start" and made["status"] == "active"
    assert made["severity"] == "high" and made["tags"] == ["a", "b"]
    assert made["body"].startswith("## Context")
    raw = json.loads(gh.state.read_text(encoding="utf-8"))["items"]["issue"][0]
    # Name and status are not written twice: the number is the key, the status a label, and the block has neither.
    assert raw["labels"] == ["status:active"]
    block = raw["body"].split("-->")[0]
    assert "name:" not in block and "status:" not in block and "title:" not in block
    cols, rows = gh.query(where="severity = 'high'")
    assert [r["name"] for r in rows] == ["1"]
    cols, rows = gh.query(sql="SELECT count(*) AS n FROM records WHERE status = 'active'")
    assert rows == [{"n": 1}]
    assert [r["name"] for r in gh.list_records(status="active")] == ["1"]
    assert gh.list_records(status="done") == []
    assert gh.get("#1")["name"] == "1"


def test_update_merges_fields_and_keeps_the_body(gh):
    gh.create("t", meta={"severity": "low", "owner": "x"}, body="Body.\n")
    out = gh.update("1", meta={"severity": "high", "owner": None, "title": "Renamed"})
    assert out["severity"] == "high" and "owner" not in out and out["title"] == "Renamed"
    assert out["body"].strip() == "Body."
    out = gh.update("1", body="New body.\n")
    assert out["severity"] == "high" and out["body"].strip() == "New body."


def test_move_swaps_the_status_label_and_closes(gh):
    gh.create("t")
    gh.move("1", "draft")
    item = json.loads(gh.state.read_text(encoding="utf-8"))["items"]["issue"][0]
    assert item["labels"] == ["status:draft"] and item["state"] == "OPEN"
    moved = gh.move("1", "done")
    assert moved["status"] == "done"
    item = json.loads(gh.state.read_text(encoding="utf-8"))["items"]["issue"][0]
    assert item["labels"] == ["status:done"] and item["state"] == "CLOSED"
    assert gh.move("1", "active")["status"] == "active"
    assert json.loads(gh.state.read_text(encoding="utf-8"))["items"]["issue"][0]["state"] == "OPEN"


def test_a_closed_issue_without_a_label_has_the_first_closed_status(gh):
    gh.create("t")
    data = json.loads(gh.state.read_text(encoding="utf-8"))
    data["items"]["issue"][0].update(labels=[], state="CLOSED")
    gh.state.write_text(json.dumps(data), encoding="utf-8")
    assert gh.get("1")["status"] == "done"


def test_delete_refuses_a_closed_record_without_force(gh):
    gh.create("t", meta={"status": "done"})
    with pytest.raises(SystemExit, match="pass --force"):
        gh.delete("1")
    gh.delete("1", force=True)
    assert gh.list_records() == []


def test_a_missing_record_and_a_bad_name_fail(gh):
    with pytest.raises(SystemExit, match="could not resolve"):
        gh.get("9")
    with pytest.raises(SystemExit, match="its number"):
        gh.get("slow-start")


def test_pull_requests_are_records_too(gh, tmp_path):
    gh.config["kind"] = "pr"
    gh.state.write_text(json.dumps({"items": {"issue": [], "pr": [
        {"number": 7, "title": "A change", "body": "Why.\n", "state": "OPEN", "labels": [], "repo": "o/r"},
    ]}, "labels": [], "next": 8}), encoding="utf-8")
    assert [r["name"] for r in gh.list_records()] == ["7"]
    assert gh.move("7", "done")["status"] == "done"
    assert any(c[:2] == ["pr", "close"] for c in calls(gh))
    with pytest.raises(SystemExit, match="opened from a branch"):
        gh.create("x")
    with pytest.raises(SystemExit, match="cannot be deleted"):
        gh.delete("7", force=True)


def test_no_credential_is_passed_or_written(gh, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    gh.create("t")
    assert all("secret-value" not in json.dumps(c) for c in calls(gh))
    assert "secret-value" not in json.dumps(gh.capabilities())
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert "secret-value" not in f.read_text(encoding="utf-8", errors="replace")


def test_gh_missing_is_a_clear_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    prov = _records.GitHubProvider({"repo": "o/r"})
    with pytest.raises(SystemExit, match="gh auth login"):
        prov.list_records()


def test_a_failing_gh_reports_its_message(gh):
    gh.config["command"] = [sys.executable, "-c", "import sys; sys.stderr.write('HTTP 401: bad credentials\\n'); sys.exit(1)"]
    with pytest.raises(SystemExit, match="bad credentials"):
        gh.list_records()


def test_commands_use_the_tracker_the_configuration_names(gh, tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("records_cmd_gh", OVERLAY / "skills" / "records" / "records.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    Records = mod.Records
    monkeypatch.setenv("AGENTS_PROJECT_ROOT", str(tmp_path))
    folder = tmp_path / ".agents" / "plans"

    assert Records.Add(folder=folder, name="First", attr=["severity=low"], body="## Notes\n\nhi\n")() == 0
    assert capsys.readouterr().out.strip().endswith("1")
    assert Records.Set(folder=folder, name="1", attrs=["severity=high"])() == 0
    assert Records.Edit(folder=folder, name="1", section="Notes", append="- more")() == 0
    assert Records.Mv(folder=folder, name="1", status="draft")() == 0
    capsys.readouterr()
    assert Records.Query(folder=folder, where="status = 'draft'", fmt="json")() == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["1"]
    assert Records.Show(folder=folder, name="1", as_json=True)() == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["severity"] == "high" and "- more" in shown["body"] and shown["status"] == "draft"
    assert not (folder / "draft").exists()
    assert Records.Rm(folder=folder, name="1")() == 0
    assert gh.list_records() == []
    assert Records.Providers(folder=folder, scope_key="plans")() == 0
    assert json.loads(capsys.readouterr().out.split("removed")[-1].split("\n", 1)[1])["kind"] == "github"
