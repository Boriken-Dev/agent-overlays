"""`decisions`: status folders, YAML frontmatter, SQL query, supersede/decide, migrate, no INDEX.md."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("duho")

OVERLAY = Path(__file__).resolve().parents[1]
_LIB = OVERLAY / "lib" / "python"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import _decisions as _dec  # noqa: E402

_cmd_spec = importlib.util.spec_from_file_location("test_decisions_cmd", OVERLAY / "skills" / "decisions" / "decisions.py")
_cmd_mod = importlib.util.module_from_spec(_cmd_spec)
sys.modules[_cmd_spec.name] = _cmd_mod
_cmd_spec.loader.exec_module(_cmd_mod)
_decisions_cmd = _cmd_mod

_D1 = "D" + "01"
_D2 = "D" + "02"
_D3 = "D" + "03"


def test_add_decide_supersede_and_no_index(tmp_path):
    store = _dec.DecisionsStore(tmp_path / "decisions")
    p1 = store.add(
        "Use frontmatter for decisions",
        description="Decisions store structured metadata in YAML frontmatter",
        status="proposed",
        provenance="Owner review",
    )
    assert p1.id == _D1
    assert p1.path == tmp_path / "decisions" / "proposed" / (_D1 + "_use_frontmatter_for_decisions.md")
    assert not (tmp_path / "decisions" / "INDEX.md").exists()

    d1 = store.set_status(_D1, "decided", note="Approved across repos.")
    assert d1.path == tmp_path / "decisions" / "decided" / (_D1 + "_use_frontmatter_for_decisions.md")
    assert d1.status == "decided"

    p2 = store.add("Status folders mirror plans", description="Store decisions under status folders")
    assert p2.id == _D2
    assert p2.path.parent.name == "decided"

    # Settled decision cannot be removed without --force
    with pytest.raises(SystemExit, match="--force"):
        store.remove(_D1)

    # Superseding requires --by and moves to superseded/
    with pytest.raises(SystemExit, match="--by"):
        store.set_status(_D1, "superseded")

    s1 = store.set_status(_D1, "superseded", by=_D2, note="Folded into replacement.")
    assert s1.status == "superseded"
    assert s1.meta["superseded_by"] == _D2
    assert s1.path == tmp_path / "decisions" / "superseded" / (_D1 + "_use_frontmatter_for_decisions.md")
    assert not (tmp_path / "decisions" / "INDEX.md").exists()


def test_sql_query_set_and_edit_decisions(tmp_path):
    store = _dec.DecisionsStore(tmp_path / "decisions")
    store.add("Choice one", status="decided", attrs={"area": "cli", "priority": 1})
    store.add("Choice two", status="proposed", attrs={"area": "ui", "priority": 2})

    store.set_attrs(_D1, {"owner": "team", "priority": 3})
    store.edit_body(_D1, append="## Notes\n\n- Verified\n")
    assert "Verified" in store.require(_D1).body

    cols, rows = store.query(
        select="id, status, area, priority",
        where="area = 'cli' AND priority >= 3",
        order_by="id asc",
    )
    assert cols == ["id", "status", "area", "priority"]
    assert rows == [{"id": _D1, "status": "decided", "area": "cli", "priority": 3}]


def test_migrate_legacy_flat_decisions_and_remove_index(tmp_path):
    root = tmp_path / "decisions"
    root.mkdir()
    (root / "INDEX.md").write_text(
        "# Decisions\n\n- [%s](%s_first.md) — First (2026-11-01, proposed)\n" % (_D1, _D1),
        encoding="utf-8",
    )
    (root / (_D1 + "_first.md")).write_text(
        "---\ndescription: First choice\n---\n\n# %s — First choice\n\nProvenance: 2026-11-01.\n" % _D1,
        encoding="utf-8",
    )
    (root / (_D2 + "_second.md")).write_text(
        "# %s — Second choice\n\nStatus: decided. Amended by [%s](%s.md).\n\n**Decision:** Keep it simple.\n"
        % (_D2, _D3, _D3),
        encoding="utf-8",
    )
    (root / (_D3 + "_old.md")).write_text(
        "# %s — Old choice\n\nStatus: superseded by %s.\n" % (_D3, _D2),
        encoding="utf-8",
    )

    store = _dec.DecisionsStore(root)
    res = store.migrate()
    assert res["removed_index"] is True
    assert not (root / "INDEX.md").exists()
    assert (root / "proposed" / (_D1 + "_first.md")).is_file()
    assert (root / "decided" / (_D2 + "_second.md")).is_file()
    assert (root / "superseded" / (_D3 + "_old.md")).is_file()
    assert store.require(_D2).meta["amended_by"] == _D3
    assert store.require(_D3).meta["superseded_by"] == _D2
    assert store.provider_info(tmp_path)["kind"] == "markdown"


def test_id_name_title_and_status_are_not_repeated_in_the_frontmatter(tmp_path):
    store = _dec.DecisionsStore(tmp_path / "decisions")
    made = store.add("Names live in files", description="One place per field")
    head = made.path.read_text(encoding="utf-8").split("---")[1]
    for key in ("id:", "name:", "title:", "status:"):
        assert key not in head, key
    again = store.require(made.name)
    assert (again.id, again.status, again.title) == (made.id, "decided", "Names live in files")
    later = store.add("Names and status live in paths")
    store.set_status(made.name, "superseded", by=later.id)
    moved = store.require(made.name)
    assert moved.status == "superseded" and moved.path.parent.name == "superseded"
    assert "status:" not in moved.path.read_text(encoding="utf-8").split("---")[1]
