"""`records` -- SQL-like query, update, edit, move and remove over any folder of
frontmatter-Markdown records (``plans/``, ``findings/``, ``decisions/``, ``kb/``, ...).

Every file has YAML frontmatter for structured fields/attributes and Markdown context
after the frontmatter. No ``INDEX.md`` is used.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Optional

from duho import Cli, Cmd, LoggingArgs, main as _duho_main


def _resolve_lib_dir() -> Path:
    import os
    for env_key in ("ENGINEERING_OVERLAY_ROOT", "AOS_OVERLAY_ROOT"):
        val = os.environ.get(env_key)
        if val and (Path(val) / "lib" / "python").is_dir():
            return Path(val) / "lib" / "python"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "lib" / "python"
        if candidate.is_dir():
            return candidate
    return here.parents[2] / "lib" / "python"


def _load_sibling(mod_name: str, filename: str):
    path = _resolve_lib_dir() / filename
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_ScopeBase = _load_sibling("_engineering_scope", "_scope.py").StandaloneScopeArgs  # type: ignore[misc, assignment]


_rec = _load_sibling("_engineering_records", "_records.py")
FolderStore = _rec.FolderStore
coerce_attr_value = _rec.coerce_attr_value
fail = _rec.fail
format_query_result = _rec.format_query_result
open_provider = _rec.open_provider


def _out(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))


def _parse_kv(pairs: "list[str]") -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for pair in pairs:
        if "=" not in pair:
            raise fail("expected KEY=VALUE, got %r" % (pair,))
        key, raw = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise fail("empty key in %r" % (pair,))
        out[key] = coerce_attr_value(raw)
    return out


class Records(LoggingArgs, Cli):
    """Query and manage folders of YAML-frontmatter Markdown records."""

    _parsername_ = "records"

    class _Base(_ScopeBase):
        folder: Path = Path(".")
        "Folder of Markdown records (or a name under <scope>/.agents/, e.g. findings, decisions)."
        ("folder",)

        def store(self) -> Any:
            target = Path(self.folder).expanduser()
            if not target.is_absolute() and not target.exists():
                if hasattr(self, "resolve_scope"):
                    cand = self.resolve_scope().agents_root / target
                    if cand.exists() or target.parts[0] in ("plans", "findings", "decisions"):
                        target = cand
                else:
                    cand = Path.cwd() / ".agents" / target
                    if cand.exists():
                        target = cand
            status_folders = None
            default_status = "active"
            if target.name == "findings" or (target / "active").is_dir() or (target / "processed").is_dir():
                status_folders = {"active": "active", "processed": "processed"}
            elif target.name == "plans" or (target / "ready").is_dir() or (target / "completed").is_dir():
                status_folders = {
                    "draft": "draft",
                    "ready": "ready",
                    "executing": "executing",
                    "on-hold": "on-hold",
                    "done": "completed",
                }
                default_status = "draft"
            return FolderStore(target, status_folders=status_folders, default_status=default_status)

    class Query(_Base):
        """SQL-like query over all records in a folder (`SELECT ... FROM records ...`)."""

        _parsername_ = "query"

        sql: str = ""
        "Optional full SQL SELECT query against table `records`."
        ("sql",)

        select: str = ""
        "Comma-separated columns to select (when no raw SQL is given)."
        ("--select", "-s")

        where: str = ""
        "SQL WHERE clause (e.g. \"status = 'active' AND severity = 'high'\")."
        ("--where", "-w")

        order_by: str = ""
        "SQL ORDER BY clause (e.g. \"created desc, name asc\")."
        ("--order",)

        group_by: str = ""
        "SQL GROUP BY clause."
        ("--group",)

        limit: int = 0
        "Maximum rows to return (0 = all)."
        ("--limit", "-n")

        fmt: str = "table"
        "Output format: table, json, tsv, md."
        ("--format", "-f")

        def __call__(self) -> int:
            store = self.store()
            cols, rows = store.query(
                sql=self.sql or None,
                select=self.select or None,
                where=self.where or None,
                order_by=self.order_by or None,
                group_by=self.group_by or None,
                limit=self.limit or None,
            )
            _out(format_query_result(cols, rows, self.fmt))
            return 0

    class Show(_Base):
        """Print one record (or a single section / JSON)."""

        _parsername_ = "show"

        name: str = ""
        "Record name."
        ("name",)

        section: str = ""
        "Print only `## <section>`."
        ("--section", "-s")

        body_only: bool = False
        "Print only the Markdown body after frontmatter."
        ("--body",)

        as_json: bool = False
        "Emit JSON."
        ("--json",)

        def __call__(self) -> int:
            rec = self.store().require(self.name)
            if self.as_json:
                _out(json.dumps(rec.as_dict(), indent=2, ensure_ascii=False) + "\n")
            elif self.section:
                _out(rec.section_text(self.section))
            elif self.body_only:
                _out(rec.body.rstrip("\n") + "\n" if rec.body else "")
            else:
                _out(rec.render())
            return 0

    class Add(_Base):
        """Create a new frontmatter-Markdown record."""

        _parsername_ = "add"

        name: str = ""
        "Record name (slug)."
        ("name",)

        status: str = ""
        "Initial status (default: store default)."
        ("--status",)

        attr: "list[str]" = []
        "Frontmatter attribute `KEY=VALUE` (repeatable)."
        ("--attr", "-a")

        body: str = ""
        "Body text (after frontmatter)."
        ("--body", "-b")

        body_file: Optional[Path] = None
        "Read body text from file."
        ("--body-file",)

        complex_dir: bool = False
        "Create as a directory record `<name>/README.md`."
        ("--complex",)

        def __call__(self) -> int:
            meta = _parse_kv(list(self.attr))
            text = self.body
            if self.body_file is not None:
                text = Path(self.body_file).expanduser().read_text(encoding="utf-8-sig")
            rec = self.store().add(
                self.name,
                meta=meta,
                body=text,
                status=self.status or None,
                complex_dir=self.complex_dir,
            )
            _out("%s\n" % rec.path)
            return 0

    class Set(_Base):
        """Update frontmatter attributes (`KEY=VALUE`) on a record."""

        _parsername_ = "set"

        name: str = ""
        "Record name."
        ("name",)

        attrs: "list[str]" = []
        "Attributes as `KEY=VALUE` (repeatable)."
        ("--attr", "-a")

        unset: "list[str]" = []
        "Attribute keys to remove."
        ("--unset", "-u")

        def __call__(self) -> int:
            updates = _parse_kv(list(self.attrs))
            if not updates and not self.unset:
                raise fail("pass --attr KEY=VALUE or --unset KEY")
            rec = self.store().set_attrs(self.name, updates, unset=list(self.unset))
            _out("%s\n" % rec.path)
            return 0

    class Edit(_Base):
        """Edit the Markdown body or a section of a record."""

        _parsername_ = "edit"

        name: str = ""
        "Record name."
        ("name",)

        section: str = ""
        "Target `## <section>` heading."
        ("--section", "-s")

        append: Optional[str] = None
        "Append text to the body or target section."
        ("--append",)

        replace: "list[str]" = []
        "Replace `OLD=NEW` in the body or target section."
        ("--replace", "-r")

        body: Optional[str] = None
        "Replace the entire Markdown body."
        ("--body", "-b")

        body_file: Optional[Path] = None
        "Replace the Markdown body from a file."
        ("--body-file",)

        def __call__(self) -> int:
            body = self.body
            if self.body_file is not None:
                body = Path(self.body_file).expanduser().read_text(encoding="utf-8-sig")
            rep = None
            if self.replace:
                if len(self.replace) != 1 or "=" not in self.replace[0]:
                    raise fail("--replace takes one `OLD=NEW` pair")
                old, new = self.replace[0].split("=", 1)
                rep = (old, new)
            rec = self.store().edit_body(
                self.name,
                body=body,
                section=self.section or None,
                append=self.append,
                replace=rep,
            )
            _out("%s\n" % rec.path)
            return 0

    class Mv(_Base):
        """Move a record to a new status/folder and update its frontmatter status."""

        _parsername_ = "mv"

        name: str = ""
        "Record name."
        ("name",)

        status: str = ""
        "Target status."
        ("status",)

        def __call__(self) -> int:
            rec = self.store().move(self.name, self.status)
            _out("%s -> %s\n" % (rec.name, rec.path))
            return 0

    class Rm(_Base):
        """Remove a record file (or complex record directory with --force)."""

        _parsername_ = "rm"

        name: str = ""
        "Record name."
        ("name",)

        force: bool = False
        "Remove even if done/processed or directory contains extra files."
        ("--force", "-f")

        def __call__(self) -> int:
            path = self.store().remove(self.name, force=self.force)
            _out("removed %s\n" % path)
            return 0

    class Providers(_Base):
        """Show the resolved record provider (default: markdown)."""

        _parsername_ = "providers"

        scope_key: str = "records"
        "Config section key (records, plans, findings)."
        ("--scope",)

        def __call__(self) -> int:
            store = self.store()
            agents_root = self.resolve_scope().agents_root if hasattr(self, "resolve_scope") else store.root.parent
            prov = open_provider(store, agents_root, scope_key=self.scope_key)
            _out(json.dumps(prov.capabilities(), indent=2) + "\n")
            return 0

    _subcommands_ = [Query, Show, Add, Set, Edit, Mv, Rm, Providers]


if __name__ == "__main__":
    raise SystemExit(_duho_main(Records))
