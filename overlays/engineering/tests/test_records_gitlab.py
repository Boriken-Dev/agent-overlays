"""The GitLab provider against a stand-in `glab` (tests/standin_glab.py): no network, no token."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

OVERLAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OVERLAY / "lib" / "python"))

import _records  # noqa: E402

STANDIN = (Path(__file__).resolve().parent / "standin_glab.py").as_posix()


@pytest.fixture
def gl(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setenv("STANDIN_STATE", str(state))
    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "records.toml").write_text(
        '[findings]\nprovider = "gitlab"\nproject = "g/p"\ncommand = ["%s", "%s"]\n'
        % (Path(sys.executable).as_posix(), STANDIN),
        encoding="utf-8",
    )
    prov = _records.open_provider(_records.FolderStore(agents / "findings"), agents, scope_key="findings")
    prov.state = state
    return prov


def raw(prov, kind="issues"):
    return json.loads(prov.state.read_text(encoding="utf-8"))[kind]


def test_create_get_query_and_update(gl):
    made = gl.create("Slow start", meta={"severity": "high"}, body="It hangs.\n")
    assert made["name"] == "1" and made["status"] == "active" and made["severity"] == "high"
    assert raw(gl)[0]["labels"] == ["status:active"]
    assert "name:" not in raw(gl)[0]["description"] and "status:" not in raw(gl)[0]["description"]
    assert [r["name"] for r in gl.query(where="severity = 'high'")[1]] == ["1"]
    out = gl.update("1", meta={"severity": None, "area": "net", "title": "Renamed"}, body=None)
    assert "severity" not in out and out["area"] == "net" and out["title"] == "Renamed"
    assert out["body"].strip() == "It hangs."


def test_move_closes_and_reopens_and_swaps_labels(gl):
    gl.create("t")
    assert gl.move("1", "processed")["status"] == "processed"
    assert raw(gl)[0]["state"] == "closed" and raw(gl)[0]["labels"] == ["status:processed"]
    assert gl.move("1", "active")["status"] == "active"
    assert raw(gl)[0]["state"] == "opened" and raw(gl)[0]["labels"] == ["status:active"]


def test_listing_pages_through_every_issue(gl):
    for i in range(103):
        gl.create("t%d" % i)
    assert len(gl.list_records()) == 103


def test_delete_needs_force_when_closed(gl):
    gl.create("t", meta={"status": "done"})
    assert raw(gl)[0]["state"] == "closed"
    with pytest.raises(SystemExit, match="pass --force"):
        gl.delete("1")
    gl.delete("1", force=True)
    assert raw(gl) == []


def test_project_path_is_encoded_and_a_wrong_project_fails(gl):
    gl.list_records()
    calls = [json.loads(l) for l in Path(str(gl.state) + ".calls").read_text(encoding="utf-8").splitlines()]
    assert calls[0][1].startswith("projects/g%2Fp/issues")
    gl.config["project"] = "other/x"
    with pytest.raises(SystemExit, match="404"):
        gl.list_records()


def test_merge_requests(gl):
    gl.config["kind"] = "mr"
    gl.state.write_text(json.dumps({"issues": [], "next": 5, "merge_requests": [
        {"iid": 4, "title": "Change", "description": "Why.\n", "state": "opened", "labels": []}]}), encoding="utf-8")
    assert [r["name"] for r in gl.list_records()] == ["4"]
    assert gl.move("4", "done")["status"] == "done"
    with pytest.raises(SystemExit, match="opened from a branch"):
        gl.create("x")
