"""`decisions` -- frontmatter-Markdown decisions log in status folders.

Layout::

    decisions/
        proposed/<Dnn_slug>.md          or proposed/<name>/README.md + extras
        decided/<Dnn_slug>.md           or decided/<name>/README.md + extras
        superseded/<Dnn_slug>.md        or superseded/<name>/README.md + extras

Metadata lives in YAML frontmatter (AI6-report style); provenance, rationale, and
context live after the frontmatter. No ``INDEX.md`` is generated or maintained.
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


_rec = _load_sibling("_records", "_records.py")
_dec = _load_sibling("_engineering_decisions", "_decisions.py")


def _out(text: str) -> None:
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.flush()
    except AttributeError:
        sys.stdout.write(text)


def _read_text_arg(inline: Optional[str], path: Optional[Path]) -> Optional[str]:
    if inline is not None and path is not None:
        raise _dec.fail("pass at most one of inline text and its -file counterpart")
    if path is not None:
        if str(path) == "-":
            return sys.stdin.buffer.read().decode("utf-8")
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise _dec.fail("cannot read %s: %s" % (path, exc)) from None
    return inline


def _parse_attrs(pairs: "list[str]") -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for pair in pairs:
        if "=" not in pair:
            raise _dec.fail("expected KEY=VALUE, got %r" % (pair,))
        k, raw = pair.split("=", 1)
        k = k.strip()
        if not k:
            raise _dec.fail("empty attribute key in %r" % (pair,))
        out[k] = _rec.coerce_attr_value(raw)
    return out


class Decisions(LoggingArgs, Cli):
    """Add, query, update, edit, decide, supersede, and migrate decisions."""

    _parsername_ = "decisions"

    class _Base(_ScopeBase):
        dir: Optional[Path] = None
        "Decisions directory (defaults to `<scope>/.agents/decisions`)."
        ("--dir",)

        def store(self) -> Any:
            if self.dir is not None:
                return _dec.DecisionsStore(self.dir.expanduser())
            if hasattr(self, "resolve_scope"):
                return _dec.DecisionsStore(self.resolve_scope().agents_root / "decisions")
            return _dec.DecisionsStore(Path.cwd() / ".agents" / "decisions")

    class _Keyed(_Base):
        key: str = ""
        "Decision ID (`D<nn>`) or stem (`D<nn>_...`)."
        ("key",)

    class Add(_Base):
        """Record a new decision (defaults to `decided/`; pass `--proposed` for a proposal)."""

        _parsername_ = "add"

        title: str = ""
        "Short human title for the decision."
        ("title",)

        id: str = ""
        "Decision ID (e.g. D<nn>; defaults to next free ID)."
        ("--id",)

        name: str = ""
        "Optional file stem (defaults to `<ID>_<slug>`)."
        ("--name", "-n")

        description: str = ""
        "One-line decision summary for frontmatter `description:`."
        ("--description", "-d")

        status: str = "decided"
        "Initial status: `decided` (default) or `proposed`."
        ("--status",)

        proposed: bool = False
        "Shorthand for `--status proposed`."
        ("--proposed",)

        provenance: str = ""
        "Who decided and when / finding source."
        ("--provenance", "-p")

        attrs: "list[str]" = []
        "Extra frontmatter `KEY=VALUE` (repeatable)."
        ("--attr", "-a")

        body: Optional[str] = None
        "Full Markdown body after frontmatter."
        ("--body", "-b")

        body_file: Optional[Path] = None
        "Read Markdown body from a file (`-` = stdin)."
        ("--body-file",)

        complex_dir: bool = False
        "Create a complex decision directory (`<name>/README.md`)."
        ("--complex",)

        def __call__(self) -> int:
            body = _read_text_arg(self.body, self.body_file) or ""
            status = "proposed" if self.proposed else self.status
            dec = self.store().add(
                self.title,
                decision_id=self.id,
                name=self.name or None,
                description=self.description,
                status=status,
                provenance=self.provenance,
                body=body,
                attrs=_parse_attrs(list(self.attrs)),
                complex_dir=self.complex_dir,
            )
            _out("%s\n" % dec.path)
            return 0

    class List(_Base):
        """List decisions (active `decided` + `proposed` by default)."""

        _parsername_ = "list"

        status: str = ""
        "Filter to one status (`proposed`, `decided`, `superseded`)."
        ("--status",)

        all_rows: bool = False
        "Include `superseded` decisions as well."
        ("--all", "-a")

        proposed: bool = False
        "Only list `proposed` decisions."
        ("--proposed",)

        superseded: bool = False
        "Only list `superseded` decisions."
        ("--superseded",)

        where: str = ""
        "SQL WHERE filter."
        ("--where", "-w")

        order_by: str = ""
        "SQL ORDER BY expression."
        ("--order",)

        as_json: bool = False
        "Emit JSON."
        ("--json",)

        def __call__(self) -> int:
            store = self.store()
            if self.where or self.order_by:
                where = self.where
                if self.status:
                    cond = "status = '%s'" % _dec.normalize_status(self.status)
                    where = "(%s) AND %s" % (where, cond) if where else cond
                elif not self.all_rows:
                    cond = "status != 'superseded'"
                    where = "(%s) AND %s" % (where, cond) if where else cond
                _, raw_rows = store.query(
                    select="*",
                    where=where or None,
                    order_by=self.order_by or "id asc, name asc",
                )
                matched = {r["name"] for r in raw_rows}
                by_name = {d.name: d for d in store.all()}
                rows = [by_name[r["name"]] for r in raw_rows if r["name"] in by_name and r["name"] in matched]
            else:
                want = None
                if self.status:
                    want = {_dec.normalize_status(self.status)}
                elif self.proposed:
                    want = {"proposed"}
                elif self.superseded:
                    want = {"superseded"}
                elif not self.all_rows:
                    want = {"proposed", "decided"}
                rows = [d for d in store.all() if want is None or d.status in want]

            if self.as_json:
                _out(json.dumps([d.as_dict() for d in rows], indent=2) + "\n")
                return 0
            for d in rows:
                _out("%s  [%-10s]  %s — %s\n" % (d.id, d.status, d.title, d.description))
            return 0

    class Query(_Base):
        """SQL-like query over decisions (`SELECT ... FROM decisions WHERE ...`)."""

        _parsername_ = "query"

        sql: str = ""
        "Full SQL SELECT query against `decisions` (or `records`)."
        ("sql",)

        select: str = ""
        "Comma-separated columns to select."
        ("--select", "-s")

        where: str = ""
        "SQL WHERE clause."
        ("--where", "-w")

        order_by: str = ""
        "SQL ORDER BY clause."
        ("--order",)

        group_by: str = ""
        "SQL GROUP BY clause."
        ("--group",)

        limit: int = 0
        "Maximum rows (0 = all)."
        ("--limit", "-n")

        fmt: str = "table"
        "Output format: table, json, tsv, md."
        ("--format", "-f")

        def __call__(self) -> int:
            cols, rows = self.store().query(
                sql=self.sql or None,
                select=self.select or None,
                where=self.where or None,
                order_by=self.order_by or None,
                group_by=self.group_by or None,
                limit=self.limit or None,
            )
            _out(_rec.format_query_result(cols, rows, self.fmt))
            return 0

    class Show(_Keyed):
        """Print one decision, whole file (or structured JSON with `--json`)."""

        _parsername_ = "show"

        as_json: bool = False
        "Emit JSON."
        ("--json",)

        def __call__(self) -> int:
            dec = self.store().require(self.key)
            if self.as_json:
                _out(json.dumps(dec.as_dict(), indent=2) + "\n")
            else:
                _out(dec.render())
            return 0

    class Set(_Keyed):
        """Update frontmatter attributes (`--attr KEY=VALUE`) on a decision."""

        _parsername_ = "set"

        attrs: "list[str]" = []
        "Attributes as `KEY=VALUE` (repeatable)."
        ("--attr", "-a")

        unset: "list[str]" = []
        "Attribute keys to remove."
        ("--unset", "-u")

        def __call__(self) -> int:
            updates = _parse_attrs(list(self.attrs))
            if not updates and not self.unset:
                raise _dec.fail("pass --attr KEY=VALUE or --unset KEY")
            dec = self.store().set_attrs(self.key, updates, unset=list(self.unset))
            _out("%s\n" % dec.path)
            return 0

    class Edit(_Keyed):
        """Edit the Markdown body or a `## <section>` of a decision."""

        _parsername_ = "edit"

        section: str = ""
        "Target `## <section>` heading."
        ("--section", "-s")

        append: Optional[str] = None
        "Append text to the body or section."
        ("--append",)

        replace: "list[str]" = []
        "Replace `OLD=NEW`."
        ("--replace", "-r")

        body: Optional[str] = None
        "Replace entire body."
        ("--body", "-b")

        body_file: Optional[Path] = None
        "Read replacement body from a file (`-` = stdin)."
        ("--body-file",)

        def __call__(self) -> int:
            body = _read_text_arg(self.body, self.body_file)
            rep = None
            if self.replace:
                if len(self.replace) != 1 or "=" not in self.replace[0]:
                    raise _dec.fail("--replace takes one `OLD=NEW` pair")
                old, new = self.replace[0].split("=", 1)
                rep = (old, new)
            dec = self.store().edit_body(
                self.key,
                body=body,
                section=self.section or None,
                append=self.append,
                replace=rep,
            )
            _out("%s\n" % dec.path)
            return 0

    class Decide(_Keyed):
        """Accept a proposed decision and move it to `decided/`."""

        _parsername_ = "decide"

        note: str = ""
        "Optional decision note."
        ("--note", "-m")

        def __call__(self) -> int:
            dec = self.store().set_status(self.key, "decided", note=self.note)
            _out("%s\n" % dec.path)
            return 0

    class Supersede(_Keyed):
        """Mark a decision superseded (`--by <Dnn>`) and move it to `superseded/`."""

        _parsername_ = "supersede"

        by: str = ""
        "Replacement decision ID (e.g. D<nn>)."
        ("--by",)

        note: str = ""
        "Reason / note explaining the supersession."
        ("--note", "-m")

        def __call__(self) -> int:
            dec = self.store().set_status(self.key, "superseded", by=self.by, note=self.note)
            _out("%s\n" % dec.path)
            return 0

    class Status(_Base):
        """Move a decision to `proposed`, `decided`, or `superseded`."""

        _parsername_ = "status"

        key: str = ""
        "Decision ID or stem."
        ("key",)

        to: str = ""
        "Target status (`proposed`, `decided`, `superseded`)."
        ("to",)

        by: str = ""
        "Replacement ID when moving to `superseded`."
        ("--by",)

        note: str = ""
        "Optional note appended to body."
        ("--note", "-m")

        def __call__(self) -> int:
            dec = self.store().set_status(self.key, self.to, by=self.by, note=self.note)
            _out("%s\n" % dec.path)
            return 0

    class Rm(_Keyed):
        """Remove a decision (`--force` required for `decided` or `superseded`)."""

        _parsername_ = "rm"

        force: bool = False
        "Allow removing a settled decision."
        ("--force", "-f")

        def __call__(self) -> int:
            path = self.store().remove(self.key, force=self.force)
            _out("removed %s\n" % path)
            return 0

    class Migrate(_Base):
        """Migrate legacy flat decisions to YAML frontmatter + status folders, removing INDEX.md."""

        _parsername_ = "migrate"

        dry_run: bool = False
        "Report changes without writing."
        ("--dry-run",)

        def __call__(self) -> int:
            res = self.store().migrate(dry_run=self.dry_run)
            _out(json.dumps(res, indent=2) + "\n")
            return 0

    class Providers(_Base):
        """Show the active decisions provider configuration (default: markdown)."""

        _parsername_ = "providers"

        def __call__(self) -> int:
            store = self.store()
            agents_root = self.resolve_scope().agents_root if hasattr(self, "resolve_scope") else store.root.parent
            _out(json.dumps(store.provider_info(agents_root), indent=2) + "\n")
            return 0

    class PathCmd(_Base):
        """Print the decisions directory."""

        _parsername_ = "path"

        def __call__(self) -> int:
            _out("%s\n" % self.store().root)
            return 0

    _subcommands_ = [
        Add, List, Query, Show, Set, Edit, Decide, Supersede, Status, Rm,
        Migrate, Providers, PathCmd,
    ]


if __name__ == "__main__":
    raise SystemExit(_duho_main(Decisions))
