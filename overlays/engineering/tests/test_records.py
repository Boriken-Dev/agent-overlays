"""Tests for shared `_records` engine and `records` command (standalone overlay skill)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("duho")

OVERLAY = Path(__file__).resolve().parents[1]
_LIB = OVERLAY / "lib" / "python"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import _records  # noqa: E402

_cmd_spec = importlib.util.spec_from_file_location("test_records_cmd", OVERLAY / "skills" / "records" / "records.py")
_cmd_mod = importlib.util.module_from_spec(_cmd_spec)
sys.modules[_cmd_spec.name] = _cmd_mod
_cmd_spec.loader.exec_module(_cmd_mod)
Records = _cmd_mod.Records


def test_frontmatter_roundtrip_and_types(tmp_path):
    store = _records.FolderStore(
        tmp_path / "findings",
        status_folders={"active": "active", "processed": "processed"},
        default_status="active",
    )
    rec = store.add(
        "proxy-gap",
        meta={"description": "[high][security] proxy bypass", "severity": "high", "tags": ["proxy", "tls"], "count": 2},
        body="Details here.\n",
    )
    assert rec.path == tmp_path / "findings" / "active" / "proxy-gap.md"
    loaded = store.require("proxy-gap")
    assert loaded.meta["severity"] == "high"
    assert loaded.meta["tags"] == ["proxy", "tls"]
    assert loaded.meta["count"] == 2
    assert loaded.meta["description"] == "[high][security] proxy bypass"
    assert "Details here." in loaded.body


def test_complex_directory_record_and_move(tmp_path):
    store = _records.FolderStore(
        tmp_path / "plans",
        status_folders={"draft": "draft", "ready": "ready", "done": "completed"},
        default_status="draft",
    )
    rec = store.add("big-plan", meta={"title": "Big plan", "priority": "high"}, body="# Big plan\n", complex_dir=True)
    assert rec.path == tmp_path / "plans" / "draft" / "big-plan" / "README.md"
    (rec.path.parent / "phase_1.md").write_text("# Phase 1\n", encoding="utf-8")

    moved = store.move("big-plan", "ready")
    assert moved.path == tmp_path / "plans" / "ready" / "big-plan" / "README.md"
    assert (tmp_path / "plans" / "ready" / "big-plan" / "phase_1.md").is_file()
    assert not (tmp_path / "plans" / "draft" / "big-plan").exists()
    assert len(store.require("big-plan").extras) == 1


def test_sql_query_is_readonly_and_supports_dynamic_frontmatter_attrs(tmp_path):
    store = _records.FolderStore(
        tmp_path / "findings",
        status_folders={"active": "active", "processed": "processed"},
        default_status="active",
    )
    store.add("one", meta={"severity": "high", "component": "host"}, body="body 1")
    store.add("two", meta={"severity": "low", "component": "app"}, body="body 2")
    store.move("two", "processed")

    cols, rows = store.query(
        sql="SELECT name, status, severity, component FROM records WHERE status = 'active' AND severity = 'high'"
    )
    assert cols == ["name", "status", "severity", "component"]
    assert rows == [{"name": "one", "status": "active", "severity": "high", "component": "host"}]

    # GROUP BY / aggregate
    cols2, rows2 = store.query(select="status, COUNT(*) AS cnt", group_by="status", order_by="status asc")
    assert rows2 == [{"status": "active", "cnt": 1}, {"status": "processed", "cnt": 1}]

    # Read-only authorizer blocks mutating SQL
    with pytest.raises(SystemExit, match="query failed"):
        store.query(sql="DELETE FROM records")


def test_set_edit_and_rm(tmp_path):
    store = _records.FolderStore(tmp_path / "notes")
    store.add("alpha", meta={"owner": "alice", "priority": 1}, body="## Facts\n\n- first\n")
    store.set_attrs("alpha", {"priority": 2, "tag": "net"}, unset=["owner"])
    rec = store.require("alpha")
    assert rec.meta["priority"] == 2
    assert rec.meta["tag"] == "net"
    assert "owner" not in rec.meta

    store.edit_body("alpha", section="Facts", append="- second")
    assert "- second" in store.require("alpha").section_text("Facts")
    store.edit_body("alpha", section="Facts", replace=("second", "updated"))
    assert "- updated" in store.require("alpha").section_text("Facts")

    removed = store.remove("alpha")
    assert not removed.exists()


def test_provider_config_resolution(tmp_path):
    agents = tmp_path / ".agents"
    agents.mkdir()
    store = _records.FolderStore(agents / "plans")
    prov = _records.open_provider(store, agents, scope_key="plans")
    assert prov.kind == "markdown"

    (agents / "records.toml").write_text(
        '[plans]\nprovider = "github"\nrepo = "Boriken-Dev/canoa"\n',
        encoding="utf-8",
    )
    gh = _records.open_provider(store, agents, scope_key="plans")
    assert gh.kind == "github"
    assert gh.capabilities()["configured"] is True
    with pytest.raises(SystemExit, match="external provider 'github'"):
        gh.list_records()


def test_records_cli_subcommands(tmp_path, capsys):
    folder = tmp_path / "kb"
    cmd_add = Records.Add(folder=folder, name="item-one", attr=["severity=high", "tags=[a,b]"], body="## Notes\n\nhello\n")
    assert cmd_add() == 0
    capsys.readouterr()

    cmd_set = Records.Set(folder=folder, name="item-one", attrs=["severity=medium"])
    assert cmd_set() == 0
    capsys.readouterr()

    cmd_q = Records.Query(folder=folder, where="severity = 'medium'", fmt="json")
    assert cmd_q() == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1 and data[0]["name"] == "item-one"

    cmd_mv = Records.Mv(folder=folder, name="item-one", status="processed")
    assert cmd_mv() == 0
    capsys.readouterr()
    assert (folder / "processed" / "item-one.md").is_file()
    assert not (folder / "item-one.md").exists()


def test_name_and_status_live_in_the_path_not_the_frontmatter(tmp_path):
    store = _records.FolderStore(
        tmp_path / "findings",
        status_folders={"active": "active", "processed": "processed"},
        default_status="active",
    )
    rec = store.add("Slow start", meta={"severity": "low"}, body="Context.\n")
    head = rec.path.read_text(encoding="utf-8").split("---")[1]
    assert "name:" not in head and "status:" not in head
    assert (rec.name, rec.status) == ("slow-start", "active")
    moved = store.move("slow-start", "processed")
    head = moved.path.read_text(encoding="utf-8").split("---")[1]
    assert "status:" not in head
    assert store.require("slow-start").status == "processed"
    # A file someone wrote with the old fields loses them on its next save.
    legacy = tmp_path / "findings" / "active" / "old.md"
    legacy.write_text("---\nname: old\nstatus: active\nseverity: high\n---\n\nBody.\n", encoding="utf-8")
    store.set_attrs("old", {"severity": "medium"})
    head = legacy.read_text(encoding="utf-8").split("---")[1]
    assert "name:" not in head and "status:" not in head and "severity: medium" in head
