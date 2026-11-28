"""`plans` -- frontmatter-Markdown plans in status folders.

Layout::

    plans/
        draft/<name>.md               or draft/<name>/README.md + <phase|extra>.md
        ready/<name>.md               or ready/<name>/README.md + ...
        review/<name>.md              or review/<name>/README.md + ...
        executing/<name>.md           or executing/<name>/README.md + ...
        on-hold/<name>.md             or on-hold/<name>/README.md + ...
        completed/<name>.md           or completed/<name>/README.md + ...

Metadata lives in YAML frontmatter (AI6-report style); context and phases live
after the frontmatter. No ``INDEX.md`` is generated or maintained.
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
_plans = _load_sibling("_engineering_plans", "_plans.py")

PlansStore = _plans.PlansStore
PLANS_DIRNAME = _plans.PLANS_DIRNAME
SECTIONS = _plans.SECTIONS
LIVE = _plans.LIVE
_fail = _plans._fail
coerce_attr_value = _rec.coerce_attr_value
format_query_result = _rec.format_query_result


def _out(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))


def _parse_attrs(pairs: "list[str]") -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for pair in pairs:
        if "=" not in pair:
            raise _fail("expected KEY=VALUE, got %r" % (pair,))
        k, v = pair.split("=", 1)
        out[k.strip()] = coerce_attr_value(v)
    return out


class Plans(LoggingArgs, Cli):
    """Plans for a project: write, query, update, step, move.

    Plans live in `<project>/.agents/plans/<status>/<name>.md` (or
    `<name>/README.md` + `<phase|extra>.md`); `--dir` overrides the location."""

    _parsername_ = "plans"

    class _Base(_ScopeBase):
        dir: Optional[Path] = None
        "Plans directory override (default: <project>/.agents/plans)."
        ("--dir",)

        def store(self) -> PlansStore:
            if self.dir:
                return PlansStore(Path(self.dir).expanduser())
            if getattr(self, "global_scope", False):
                raise _fail("plans belong to a project; there is no user-store plan queue")
            if hasattr(self, "resolve_scope"):
                return PlansStore(self.resolve_scope().agents_root / PLANS_DIRNAME)
            return PlansStore(Path.cwd() / ".agents" / PLANS_DIRNAME)

    class _Named(_Base):
        name: str = ""
        "Plan name (`<name>` or `<parent>/<sub>`)."
        ("name",)

    class Add(_Named):
        """Create a plan from the template in `plans/<status>/`."""

        _parsername_ = "add"

        title: str = ""
        "The plan's title."
        ("--title", "-t")

        executor: str = ""
        "The executor role from MODELS.md and why."
        ("--executor", "-e")

        phase: "list[str]" = []
        "A phase name; repeat for each phase, in order."
        ("--phase", "-p")

        status: str = "draft"
        "Initial status (draft, ready, review, executing)."
        ("--status",)

        complex_dir: bool = False
        "Create a complex directory plan `<name>/README.md`."
        ("--complex",)

        attr: "list[str]" = []
        "Extra frontmatter attribute `KEY=VALUE` (repeatable)."
        ("--attr", "-a")

        def __call__(self) -> int:
            attrs = _parse_attrs(list(self.attr)) if self.attr else None
            plan = self.store().add(
                self.name,
                title=self.title,
                executor=self.executor,
                phases=list(self.phase),
                status=self.status,
                complex_dir=self.complex_dir,
                attrs=attrs,
            )
            _out("%s\n" % plan.path)
            return 0

    class List(_Base):
        """List plans (live by default; supports --status, --where, --order, --json)."""

        _parsername_ = "list"

        all: bool = False
        "Include on-hold and completed plans."
        ("--all", "-a")

        completed: bool = False
        "Only completed plans."
        ("--completed",)

        status: str = ""
        "Filter to one status (draft, ready, review, executing, on-hold, done)."
        ("--status",)

        where: str = ""
        "SQL WHERE filter over plan fields."
        ("--where", "-w")

        order_by: str = ""
        "SQL ORDER BY clause."
        ("--order",)

        as_json: bool = False
        "Emit JSON."
        ("--json",)

        def __call__(self) -> int:
            store = self.store()
            if self.where or self.order_by:
                where = self.where
                if self.status:
                    cond = "status = '%s'" % self.status.replace("'", "''")
                    where = ("(%s) AND (%s)" % (cond, where)) if where else cond
                elif self.completed:
                    where = ("(status = 'done') AND (%s)" % where) if where else "status = 'done'"
                elif not self.all:
                    live_cond = "status IN ('draft', 'ready', 'review', 'executing')"
                    where = ("(%s) AND (%s)" % (live_cond, where)) if where else live_cond
                cols, rows = store.query(where=where, order_by=self.order_by or None)
                _out(format_query_result(cols, rows, "json" if self.as_json else "table"))
                return 0

            plans = store.completed() if self.completed else store.all() if (self.all or self.status) else store.live()
            if self.status:
                plans = [p for p in plans if p.status == self.status.strip().lower()]
            if self.as_json:
                _out(json.dumps({"dir": str(store.root),
                                 "plans": [p.as_dict() for p in plans]}, indent=2) + "\n")
                return 0
            for plan in plans:
                _out(plan.summary() + "\n")
            return 0

    class Query(_Base):
        """SQL-like query over plans (`SELECT ... FROM plans WHERE ...`)."""

        _parsername_ = "query"

        sql: str = ""
        "Full SQL SELECT query against table `plans` (or `records`)."
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
        "Max rows (0 = all)."
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
            _out(format_query_result(cols, rows, self.fmt))
            return 0

    class Show(_Named):
        """Print a plan, or just one part of it (a section, a phase, or the brief)."""

        _parsername_ = "show"

        section: str = ""
        "One section: head, progress, facts, phases, verification, reporting (or a heading)."
        ("--section", "-s")

        phase: str = ""
        "One phase: its Progress line and its `### Phase` block."
        ("--phase", "-p")

        brief: bool = False
        "Only the frontmatter, the title and the Progress lines."
        ("--brief", "-b")

        as_json: bool = False
        "Emit structured JSON."
        ("--json",)

        def __call__(self) -> int:
            plan = self.store().require(self.name)
            if self.as_json:
                _out(json.dumps(plan.as_dict(), indent=2) + "\n")
            elif self.phase:
                _out(plan.phase_view(self.phase))
            elif self.brief:
                _out(plan.head_text() + "\n" + plan.section_text(SECTIONS["progress"]))
            elif self.section.strip().lower() == "head":
                _out(plan.head_text())
            elif self.section:
                _out(plan.section_text(plan.resolve_heading(self.section)))
            else:
                _out(plan.render())
            return 0

    class Phases(_Named):
        """List the phases: id, state, title (`show --phase N` reads one)."""

        _parsername_ = "phases"

        open_only: bool = False
        "Only the phases not yet checked."
        ("--open",)

        fmt: str = "table"
        "Output format: table or json."
        ("--format", "-f")

        def __call__(self) -> int:
            plan = self.store().require(self.name)
            items = [it for it in plan.items() if not (self.open_only and it.box == "x")]
            if self.fmt == "json":
                _out(json.dumps([{"id": it.id, "state": it.state, "title": it.name, "note": it.note}
                                 for it in items], indent=2) + "\n")
            elif self.fmt == "table":
                for it in items:
                    _out("%s  %-7s  %s%s\n" % (it.id, it.state, it.name, " -- " + it.note if it.note else ""))
            else:
                raise _fail("--format is table or json")
            return 0

    class Next(_Named):
        """Print what an executor needs now (head, progress, and current phase)."""

        _parsername_ = "next"

        block_only: bool = False
        "Only the current phase's `### Phase` block."
        ("--block",)

        @staticmethod
        def _nothing_open(plan, blocked) -> str:
            if not blocked:
                return "%s: every phase is checked" % plan.name
            return ("%s: no phase is open and the plan is not finished; blocked: %s. "
                    "Unblock it and `plans start %s <phase>`, or park the plan with "
                    "`plans status %s on-hold -m \"<reason>\"`."
                    % (plan.name,
                       "; ".join("Phase %s: %s%s" % (b.id, b.name, " -- " + b.note if b.note else "")
                                 for b in blocked),
                       plan.name, plan.name))

        def __call__(self) -> int:
            plan = self.store().require(self.name)
            current = plan.current()
            blocked = [it for it in plan.items() if it.box == "!"]
            if self.block_only:
                if current is None:
                    raise _fail(self._nothing_open(plan, blocked))
                _out(plan.phase_text(current.id))
                return 0
            _out(plan.head_text() + "\n")
            _out(plan.section_text(SECTIONS["progress"]) + "\n")
            if current is None and blocked:
                _out(self._nothing_open(plan, blocked) + "\n")
            elif current is None:
                _out("Every phase is checked. Close the plan: "
                     "plans status %s done -m \"<evidence>\"\n" % plan.name)
            else:
                _out(plan.phase_text(current.id))
            return 0

    class _Mark(_Named):
        phase: str = ""
        "Phase id, as the Progress list shows it."
        ("phase",)

        message: str = ""
        "A short outcome or reason, kept on the progress line."
        ("--message", "-m")

        BOX = " "
        NEEDS_MESSAGE = False

        def __call__(self) -> int:
            store = self.store()
            plan = store.require(self.name)
            if self.NEEDS_MESSAGE and not self.message.strip():
                raise _fail("pass -m: %s" % self.NEEDS_MESSAGE)
            if plan.status not in LIVE:
                raise _fail("%s is %s; set it live first (`plans status %s executing`)"
                            % (plan.name, plan.status or "without a status", plan.name))
            note = self.message if self.message.strip() else ("" if self.BOX == " " else None)
            item = plan.mark(self.phase, self.BOX, note)
            if self.BOX == "/" and plan.status == "ready":
                store.save(plan)
                store.set_status(plan.name, "executing")
            else:
                store.save(plan)
            done, total = plan.counts()
            _out("%s: %s (%d/%d done)\n" % (plan.name, item.render()[2:], done, total))
            return 0

    class Start(_Mark):
        """Mark a phase active, before the first edit. A ready plan moves to executing/."""

        _parsername_ = "start"
        BOX = "/"

    class Check(_Mark):
        """Mark a phase done, with its outcome, as soon as it is."""

        _parsername_ = "check"
        BOX = "x"
        NEEDS_MESSAGE = "the outcome, in a few words"

    class Block(_Mark):
        """Mark a phase blocked, with the reason and the command that failed."""

        _parsername_ = "block"
        BOX = "!"
        NEEDS_MESSAGE = "why it is blocked"

    class Uncheck(_Mark):
        """Put a phase back to pending."""

        _parsername_ = "uncheck"
        BOX = " "

    class Note(_Named):
        """Append one fact to Known Facts & Context, or (with --phase) a note under a phase's block."""

        _parsername_ = "note"

        message: str = ""
        "The fact or note, with its evidence."
        ("--message", "-m")

        phase: str = ""
        "Put the note under this phase's block instead of in Known Facts."
        ("--phase", "-p")

        def __call__(self) -> int:
            if not self.message.strip():
                raise _fail("pass -m: the fact to record")
            store = self.store()
            if self.phase:
                plan, it = store.note_phase(self.name, self.phase, self.message)
                _out("%s: noted under Phase %s\n" % (plan.name, it.id))
                return 0
            plan = store.require(self.name)
            plan.append_fact(self.message)
            store.save(plan)
            _out("%s: noted\n" % plan.name)
            return 0

    class Set(_Named):
        """Update frontmatter attributes on a plan (`--attr KEY=VALUE`)."""

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
                raise _fail("pass --attr KEY=VALUE or --unset KEY")
            plan = self.store().set_attrs(self.name, updates, unset=list(self.unset))
            _out("%s\n" % plan.path)
            return 0

    class Edit(_Named):
        """Edit one section of a plan (`--section S --set|--append|--replace`), or the whole body (`--whole`)."""

        _parsername_ = "edit"

        section: str = ""
        "Target section: progress, facts, phases, verification, reporting, or a heading."
        ("--section", "-s")

        set_text: Optional[str] = None
        "Replace the section's content with this text (needs --section)."
        ("--set",)

        set_file: Optional[Path] = None
        "Replace the section's content with this file's text (needs --section)."
        ("--set-file",)

        append: Optional[str] = None
        "Append text to the section (or to the body's end without --section)."
        ("--append",)

        append_file: Optional[Path] = None
        "Append this file's text, as --append."
        ("--append-file",)

        replace: "list[str]" = []
        "Replace exact text `OLD=NEW`, within the section when --section is given."
        ("--replace", "-r")

        body: Optional[str] = None
        "Replace the WHOLE body with this text (needs --whole)."
        ("--body", "-b")

        body_file: Optional[Path] = None
        "Replace the WHOLE body with this file's text (needs --whole)."
        ("--body-file",)

        whole: bool = False
        "Confirm that --body/--body-file replace the entire plan body."
        ("--whole",)

        @staticmethod
        def _text(inline: Optional[str], path: Optional[Path], what: str) -> Optional[str]:
            if inline is not None and path is not None:
                raise _fail("pass --%s or --%s-file, not both" % (what, what))
            if path is not None:
                return Path(path).expanduser().read_text(encoding="utf-8-sig")
            return inline

        def __call__(self) -> int:
            body = self._text(self.body, self.body_file, "body")
            set_text = self._text(self.set_text, self.set_file, "set")
            append = self._text(self.append, self.append_file, "append")
            rep = None
            if self.replace:
                if len(self.replace) != 1 or "=" not in self.replace[0]:
                    raise _fail("--replace takes one `OLD=NEW` pair")
                old, new = self.replace[0].split("=", 1)
                rep = (old, new)
            if sum(x is not None for x in (body, set_text, append, rep)) > 1:
                raise _fail("one edit at a time: --set, --append, --replace or --whole --body")
            plan = self.store().edit_body(
                self.name,
                body=body,
                section=self.section or None,
                set_text=set_text,
                append=append,
                replace=rep,
                whole=self.whole,
            )
            _out("%s\n" % plan.path)
            return 0

    class PhaseAdd(_Named):
        """Add a phase: its Progress line and its `### Phase` block; later phases are renumbered."""

        _parsername_ = "phase-add"

        title: str = ""
        "The new phase's title."
        ("title",)

        after: Optional[str] = None
        "Insert after this phase id (0 = first); default: at the end."
        ("--after",)

        def __call__(self) -> int:
            plan, number = self.store().add_phase(self.name, self.title, self.after)
            _out("%s: added Phase %s; fill its block with plans edit %s --section phases ...\n"
                 % (plan.name, number, plan.name))
            return 0

    class PhaseRename(_Named):
        """Change a phase's title in Progress and in its block heading."""

        _parsername_ = "phase-rename"

        phase: str = ""
        "Phase id."
        ("phase",)

        title: str = ""
        "The new title."
        ("title",)

        def __call__(self) -> int:
            plan, number = self.store().rename_phase(self.name, self.phase, self.title)
            _out("%s: Phase %s renamed\n" % (plan.name, number))
            return 0

    class PhaseRemove(_Named):
        """Remove a phase (Progress line and block); later phases are renumbered."""

        _parsername_ = "phase-remove"

        phase: str = ""
        "Phase id."
        ("phase",)

        force: bool = False
        "Remove a checked phase."
        ("--force",)

        def __call__(self) -> int:
            plan, gone = self.store().remove_phase(self.name, self.phase, force=self.force)
            _out("%s: removed Phase %s\n" % (plan.name, gone))
            return 0

    class PhaseSplit(_Named):
        """Split one phase into several, in place: the first keeps the box and block, the rest start pending."""

        _parsername_ = "phase-split"

        phase: str = ""
        "Phase id."
        ("phase",)

        titles: "list[str]" = []
        "Two or more titles, in order."
        ("titles",)

        def __call__(self) -> int:
            plan, ids = self.store().split_phase(self.name, self.phase, list(self.titles))
            _out("%s: Phase %s is now Phases %s\n" % (plan.name, self.phase, ", ".join(ids)))
            return 0

    class Status(_Named):
        """Show a plan's status, or set it and move the plan into `plans/<status>/`."""

        _parsername_ = "status"

        value: str = ""
        "draft, ready, review, executing, on-hold or done. Omit to show."
        ("value",)

        message: str = ""
        "Evidence for `done`; the reason for `on-hold`."
        ("--message", "-m")

        force: bool = False
        "Close a plan that still has unchecked phases."
        ("--force",)

        def __call__(self) -> int:
            store = self.store()
            if not self.value:
                _out(store.require(self.name).summary() + "\n")
                return 0
            plan = store.set_status(self.name, self.value, note=self.message, force=self.force)
            _out("%s: %s -> %s\n" % (plan.name, plan.status, plan.path))
            return 0

    class Rm(_Named):
        """Remove a plan file (or complex plan directory with --force)."""

        _parsername_ = "rm"

        force: bool = False
        "Remove even if done or directory has extra files."
        ("--force", "-f")

        def __call__(self) -> int:
            path = self.store().remove(self.name, force=self.force)
            _out("removed %s\n" % path)
            return 0

    class Validate(_Named):
        """Check a plan against the required shape; with no name, every plan. Exit 1 lists what is wrong."""

        _parsername_ = "validate"

        def __call__(self) -> int:
            store = self.store()
            plans = [store.require(self.name)] if self.name else sorted(store.all(), key=lambda p: p.name)
            bad = 0
            for plan in plans:
                problems = plan.problems()
                bad += bool(problems)
                for problem in problems:
                    _out("%s: %s\n" % (plan.name, problem))
                if not problems and self.name:
                    _out("%s: ok\n" % plan.name)
            if not self.name:
                _out("%d plans, %d with problems\n" % (len(plans), bad))
            return 1 if bad else 0

    class Migrate(_Base):
        """Migrate legacy flat plans and body metadata to frontmatter + status folders, removing INDEX.md."""

        _parsername_ = "migrate"

        dry_run: bool = False
        "Report changes without writing."
        ("--dry-run",)

        def __call__(self) -> int:
            res = self.store().migrate(dry_run=self.dry_run)
            _out(json.dumps(res, indent=2) + "\n")
            return 0

    class Providers(_Base):
        """Show the active plans provider configuration (default: markdown)."""

        _parsername_ = "providers"

        def __call__(self) -> int:
            store = self.store()
            agents_root = self.resolve_scope().agents_root if hasattr(self, "resolve_scope") else store.root.parent
            _out(json.dumps(store.provider_info(agents_root), indent=2) + "\n")
            return 0

    class PathCmd(_Base):
        """Print the plans directory."""

        _parsername_ = "path"

        def __call__(self) -> int:
            _out("%s\n" % self.store().root)
            return 0

    _subcommands_ = [Add, List, Query, Show, Phases, Next, Start, Check, Block, Uncheck,
                     Note, Set, Edit, PhaseAdd, PhaseRename, PhaseRemove, PhaseSplit, Status, Rm, Validate, Migrate, Providers, PathCmd]


if __name__ == "__main__":
    raise SystemExit(_duho_main(Plans))
