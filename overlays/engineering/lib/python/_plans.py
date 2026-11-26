"""Plans as frontmatter-Markdown records organized in status folders.

Layout::

    plans/
        draft/<name>.md               or draft/<name>/README.md + <phase|extra>.md
        ready/<name>.md               or ready/<name>/README.md + ...
        review/<name>.md              or review/<name>/README.md + ...
        executing/<name>.md           or executing/<name>/README.md + ...
        on-hold/<name>.md             or on-hold/<name>/README.md + ...
        completed/<name>.md           or completed/<name>/README.md + ...

Metadata lives in YAML frontmatter (``name``, ``title``, ``status``, ``executor``,
``created``, ``updated``, ``closed``, ``outcome``, ``held``, plus any extra attrs);
the Markdown narrative and phases live after the frontmatter. No ``INDEX.md`` is
maintained.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from _records import (
    Record,
    coerce_attr_value,
    fail as _fail,
    format_query_result,
    open_provider,
    parse_frontmatter,
    query_records,
    render_frontmatter,
    today_iso as _today,
    write_text_lf,
    FolderStore,
)

PLANS_DIRNAME = "plans"
COMPLETED = "completed"
ON_HOLD = "on-hold"
INDEX_NAME = "INDEX.md"
_NOT_PLANS = {INDEX_NAME.lower(), "readme.md"}

LIVE = ("draft", "ready", "review", "executing")
STATUSES = LIVE + ("on-hold", "done")
#: Folder under ``plans/`` where each status lives.
STATUS_DIRS: "dict[str, str]" = {
    "draft": "draft",
    "ready": "ready",
    "review": "review",
    "executing": "executing",
    "on-hold": ON_HOLD,
    "done": COMPLETED,
}
_DIR_TO_STATUS = {v: k for k, v in STATUS_DIRS.items()}

BOXES = {" ": "pending", "/": "active", "x": "done", "!": "blocked"}
_SEP = " — "
_ITEM_RE = re.compile(r"^- \[([ /x!])\] (.*)$")
_PHASE_ID_RE = re.compile(r"^Phase\s+([0-9A-Za-z.]+)\s*:\s*(.*)$")
_BLOCK_HEAD_RE = re.compile(r"^###\s+Phase\s+([0-9A-Za-z.]+)\s*:\s*(.*)$", re.IGNORECASE)
_FIELD_RE = re.compile(r"^([A-Z][A-Za-z-]*):\s*(.*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->\n?", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<[a-z][^<>\n]*>")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")

_PLAN_KEY_ORDER = (
    "name", "title", "status", "executor", "priority", "tags",
    "created", "updated", "closed", "outcome", "held", "provider",
)

SECTIONS = {
    "progress": "Progress",
    "facts": "Known Facts & Context",
    "phases": "Phases",
    "verification": "Verification",
    "reporting": "Reporting",
}
_TEMPLATE = Path(__file__).resolve().parents[2] / "references" / "plan_template.md"
_FALLBACK_TEMPLATE = (
    "---\nname: <name>\ntitle: <Plan Title>\nstatus: draft\n"
    "executor: <family/subrole from MODELS.md>\n---\n\n"
    "# <Plan Title>\n\n"
    "## Progress\n\n- [ ] Phase 1: <name>\n\n## Known Facts & Context\n\n- <fact>\n\n"
    "## Phases\n\n### Phase 1: <name>\n\n- Files: <paths>\n- Guidance: <hints>\n"
    "- Why: <one line>\n- Done-when: <observable check>\n\n## Verification\n\n"
    "- <command> → <expected>\n\n## Reporting\n\n- Handoff includes: changed files, "
    "evidence per done-when, unverified items.\n"
)

_LOGGER = logging.getLogger("engineering.plans")

_LEGACY_FIELD_MAP = {
    "Status": "status",
    "Executor": "executor",
    "Closed": "closed",
    "Outcome": "outcome",
    "Held": "held",
}


def _prose_lines(lines):
    """``lines`` without fenced code blocks and backtick spans."""
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield _CODE_SPAN_RE.sub("", line)


def slugify(text: str) -> str:
    """A plan's name from free text: lowercase snake_case."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class Item:
    """One line of a plan's Progress list."""

    __slots__ = ("index", "box", "id", "name", "note")

    def __init__(self, index: int, box: str, text: str, ordinal: int):
        self.index = index
        self.box = box
        head, sep, note = text.partition(_SEP)
        match = _PHASE_ID_RE.match(head.strip())
        self.id = match.group(1) if match else str(ordinal)
        self.name = (match.group(2) if match else head).strip()
        self.note = note.strip() if sep else ""

    @property
    def state(self) -> str:
        return BOXES[self.box]

    def render(self) -> str:
        text = "- [%s] Phase %s: %s" % (self.box, self.id, self.name)
        return text + (_SEP + self.note if self.note else "")


def _split_legacy_head_fields(lines: "list[str]") -> "tuple[dict[str, str], list[str]]":
    """Extract legacy ``Status: ...`` / ``Executor: ...`` lines before the first ``## `` heading."""
    extracted: "dict[str, str]" = {}
    head_end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            head_end = i
            break
    kept: "list[str]" = []
    for i, line in enumerate(lines):
        if i < head_end:
            match = _FIELD_RE.match(line)
            if match and match.group(1) in _LEGACY_FIELD_MAP:
                key = _LEGACY_FIELD_MAP[match.group(1)]
                extracted[key] = match.group(2).strip()
                continue
        kept.append(line)
    # Collapse consecutive blank lines created by removing head fields
    cleaned: "list[str]" = []
    for line in kept:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    return extracted, cleaned


class Plan:
    """One plan (``<status>/<name>.md`` or ``<status>/<name>/README.md`` + extras)."""

    def __init__(
        self,
        path: Path,
        lines: "list[str]",
        name: str,
        *,
        meta: "Optional[dict[str, Any]]" = None,
        extras: "Optional[list[Path]]" = None,
        folder_status: str = "",
    ):
        self.path = Path(path)
        self.lines = list(lines)
        self.name = name
        self.meta: "dict[str, Any]" = dict(meta or {})
        self.extras: "list[Path]" = list(extras or [])
        # The status folder the plan sits in; empty for a legacy plan at the root, whose frontmatter still says it.
        self.folder_status = folder_status
        # The name is the file's and the status the folder's: neither is kept in the frontmatter.
        self.meta.pop("name", None)
        if folder_status:
            self.meta.pop("status", None)

    @property
    def is_complex(self) -> bool:
        return self.path.name.lower() == "readme.md"

    @classmethod
    def load(cls, path: Path, name: str, *, default_status: str = "", folder_status: str = "") -> "Plan":
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        meta, body, had_fm = parse_frontmatter(text, typed=True)
        body_lines = body.splitlines()
        legacy, cleaned_lines = _split_legacy_head_fields(body_lines)
        for k, v in legacy.items():
            if v and not meta.get(k):
                meta[k] = v
        if not folder_status and not meta.get("status") and default_status:
            meta["status"] = default_status
        # The title is the `# ` heading; frontmatter holds one only for a plan that has no heading.
        if any(line.startswith("# ") for line in cleaned_lines):
            meta.pop("title", None)
        if had_fm:
            # Keep body lines clean without duplicate legacy fields
            body_lines = cleaned_lines
        extras: "list[Path]" = []
        if path.name.lower() == "readme.md":
            for item in sorted(path.parent.rglob("*.md")):
                if item == path or item.name.lower() == INDEX_NAME.lower():
                    continue
                if "reviews" in item.relative_to(path.parent).parts:
                    continue
                extras.append(item)
        return cls(path, body_lines, name, meta=meta, extras=extras, folder_status=folder_status)

    def render(self) -> str:
        meta = dict(self.meta)
        meta.pop("name", None)
        if self.folder_status:
            meta.pop("status", None)
        elif self.status:
            meta["status"] = self.status
        _, cleaned = _split_legacy_head_fields(self.lines)
        if any(line.startswith("# ") for line in cleaned):
            meta.pop("title", None)
        body = "\n".join(cleaned).rstrip("\n") + "\n"
        return render_frontmatter(meta, body, key_order=_PLAN_KEY_ORDER)

    def save(self) -> None:
        _, self.lines = _split_legacy_head_fields(self.lines)
        write_text_lf(self.path, self.render())

    # -- metadata -------------------------------------------------------------

    @property
    def title(self) -> str:
        for line in self.lines:
            if line.startswith("# "):
                return line[2:].strip()
        if self.meta.get("title"):
            return str(self.meta["title"])
        return self.name

    def _head_end(self) -> int:
        for i, line in enumerate(self.lines):
            if line.startswith("## "):
                return i
        return len(self.lines)

    def field(self, key: str) -> str:
        lk = key.lower()
        if lk in self.meta and self.meta[lk] not in (None, ""):
            return str(self.meta[lk])
        if key in self.meta and self.meta[key] not in (None, ""):
            return str(self.meta[key])
        for line in self.lines[: self._head_end()]:
            match = _FIELD_RE.match(line)
            if match and match.group(1).lower() == lk:
                return match.group(2).strip()
        return ""

    def set_field(self, key: str, value: Optional[str]) -> None:
        lk = key.lower()
        if value is None:
            self.meta.pop(lk, None)
            self.meta.pop(key, None)
        else:
            self.meta[lk] = value
        # Remove any legacy line in body
        end = self._head_end()
        self.lines[:end] = [
            ln for ln in self.lines[:end]
            if not (_FIELD_RE.match(ln) and _FIELD_RE.match(ln).group(1).lower() == lk)
        ]

    @property
    def status(self) -> str:
        if self.folder_status:
            return self.folder_status
        raw = self.field("status")
        return raw.split()[0].lower() if raw else ""

    def head_text(self) -> str:
        """Frontmatter + title summary for `show --section head` and `next`."""
        meta_lines = ["---"]
        for k in _PLAN_KEY_ORDER:
            v = self.meta.get(k)
            if v not in (None, "", []):
                meta_lines.append("%s: %s" % (k, v))
        for k, v in self.meta.items():
            if k not in _PLAN_KEY_ORDER and v not in (None, "", []):
                meta_lines.append("%s: %s" % (k, v))
        meta_lines.append("---")
        return "\n".join(meta_lines) + "\n\n# " + self.title + "\n"

    # -- sections -------------------------------------------------------------

    def section(self, heading: str) -> "tuple[int, int]":
        start = -1
        for i, line in enumerate(self.lines):
            if line.startswith("## "):
                if start >= 0:
                    return start, i
                if line[3:].strip() == heading:
                    start = i + 1
        return (start, len(self.lines)) if start >= 0 else (-1, -1)

    def section_text(self, heading: str) -> str:
        start, end = self.section(heading)
        if start < 0:
            raise _fail("%s has no `## %s` section" % (self.name, heading))
        return "\n".join(["## " + heading] + self.lines[start:end]).rstrip() + "\n"

    # -- progress -------------------------------------------------------------

    def items(self) -> "list[Item]":
        start, end = self.section(SECTIONS["progress"])
        found: "list[Item]" = []
        for i in range(max(start, 0), max(end, 0)):
            match = _ITEM_RE.match(self.lines[i])
            if match:
                found.append(Item(i, match.group(1), match.group(2), len(found) + 1))
        return found

    def item(self, phase: str) -> Item:
        wanted = phase.strip().lower()
        if wanted.startswith("phase"):
            wanted = wanted[5:].strip().rstrip(":").strip()
        items = self.items()
        for it in items:
            if it.id.lower() == wanted:
                return it
        raise _fail("%s has no phase %r (it has: %s)"
                    % (self.name, phase, ", ".join(it.id for it in items) or "none"))

    def mark(self, phase: str, box: str, note: Optional[str]) -> Item:
        it = self.item(phase)
        it.box = box
        if note is not None:
            it.note = " ".join(note.split())
        self.lines[it.index] = it.render()
        self.meta["updated"] = _today()
        return it

    def counts(self) -> "tuple[int, int]":
        items = self.items()
        return sum(1 for it in items if it.box == "x"), len(items)

    def current(self) -> Optional[Item]:
        items = self.items()
        for box in ("/", " "):
            for it in items:
                if it.box == box:
                    return it
        return None

    def phase_text(self, phase: str) -> str:
        it = self.item(phase)
        start, end = self.section(SECTIONS["phases"])
        head = re.compile(r"^###\s+Phase\s+%s\s*:" % re.escape(it.id), re.IGNORECASE)
        for i in range(max(start, 0), max(end, 0)):
            if head.match(self.lines[i]):
                stop = i + 1
                while stop < end and not self.lines[stop].startswith("### "):
                    stop += 1
                return "\n".join(self.lines[i:stop]).rstrip() + "\n"
        # Check for sibling phase file in complex plans
        if self.is_complex:
            for extra in self.extras:
                if extra.stem.lower() in (f"phase_{it.id.lower()}", f"phase-{it.id.lower()}", it.id.lower()):
                    return extra.read_text(encoding="utf-8-sig").rstrip() + "\n"
        raise _fail("%s has no `### Phase %s:` block" % (self.name, it.id))

    def append_fact(self, fact: str) -> None:
        start, end = self.section(SECTIONS["facts"])
        if start < 0:
            raise _fail("%s has no `## %s` section" % (self.name, SECTIONS["facts"]))
        at = end
        while at > start and not self.lines[at - 1].strip():
            at -= 1
        self.lines.insert(at, "- " + " ".join(fact.split()))
        self.meta["updated"] = _today()

    def set_phases(self, phases: "list[str]") -> None:
        start, end = self.section(SECTIONS["progress"])
        self.lines[start:end] = (
            [""] + ["- [ ] Phase %d: %s" % (n, p) for n, p in enumerate(phases, 1)] + [""])
        start, end = self.section(SECTIONS["phases"])
        blocks = [""]
        for n, p in enumerate(phases, 1):
            blocks += self._template_block(n, p) + [""]
        self.lines[start:end] = blocks

    @staticmethod
    def _template_block(number: "int | str", title: str) -> "list[str]":
        return ["### Phase %s: %s" % (number, title), "", "- Files: <paths>",
                "- Guidance: <signatures, hints, defaults>", "- Why: <one line>",
                "- Done-when: <observable check>"]

    # -- named sections, whole or in part -------------------------------------

    def headings(self) -> "list[str]":
        return [line[3:].strip() for line in self.lines if line.startswith("## ")]

    def resolve_heading(self, name: str) -> str:
        """The `## ` heading a section key (`facts`) or heading text names; an unknown one lists the plan's sections."""
        wanted = name.strip().lower()
        existing = self.headings()
        for heading in existing:
            if heading.lower() == wanted:
                return heading
        key = SECTIONS.get(wanted)
        if key and key in existing:
            return key
        known = ["%s (%s)" % (k, v) for k, v in SECTIONS.items() if v in existing]
        known += [h for h in existing if h not in SECTIONS.values()]
        raise _fail("%s has no section %r (it has: %s)" % (self.name, name, ", ".join(known) or "none"))

    @staticmethod
    def _check_text(text: str) -> "list[str]":
        lines = text.rstrip("\r\n").splitlines()
        fenced = False
        for line in lines:
            if line.lstrip().startswith("```"):
                fenced = not fenced
            elif not fenced and line.startswith("## "):
                raise _fail("the text holds a `## ` heading line (%r), which would start a new section; "
                            "write it as `### ` or lower, or edit the plan file" % line)
        return lines

    def set_section(self, heading: str, text: str) -> None:
        """Replace everything under `## heading`; the heading line stays."""
        lines = self._check_text(text)
        start, end = self.section(heading)
        self.lines[start:end] = [""] + lines + [""]
        self.meta["updated"] = _today()

    def append_section(self, heading: str, text: str) -> None:
        lines = self._check_text(text)
        start, end = self.section(heading)
        at = end
        while at > start and not self.lines[at - 1].strip():
            at -= 1
        self.lines[at:at] = lines
        self.meta["updated"] = _today()

    # -- phases: reading, notes, structure ------------------------------------

    def phase_view(self, phase: str) -> str:
        """A phase's Progress line, then its `### Phase` block."""
        it = self.item(phase)
        return it.render() + "\n\n" + self.phase_text(it.id)

    def _block_end(self, phase_id: str) -> int:
        start, end = self.section(SECTIONS["phases"])
        head = re.compile(r"^###\s+Phase\s+%s\s*:" % re.escape(phase_id), re.IGNORECASE)
        for i in range(max(start, 0), max(end, 0)):
            if head.match(self.lines[i]):
                stop = i + 1
                while stop < end and not self.lines[stop].startswith("### "):
                    stop += 1
                return stop
        raise _fail("%s has no `### Phase %s:` block in its body%s" % (
            self.name, phase_id, " (a complex plan keeps it in its own file)" if self.is_complex else ""))

    def append_phase_note(self, phase: str, text: str) -> Item:
        it = self.item(phase)
        at = self._block_end(it.id)
        while at > 0 and not self.lines[at - 1].strip():
            at -= 1
        self.lines.insert(at, "- Note: " + " ".join(text.split()))
        self.meta["updated"] = _today()
        return it

    def _phase_layout(self, *, numeric: bool):
        items = self.items()
        if not items:
            raise _fail("%s has no phases in Progress" % self.name)
        if numeric and not all(it.id.isdigit() for it in items):
            raise _fail("%s numbers its phases %s; renumbering needs plain numbers, edit the file by hand"
                        % (self.name, ", ".join(it.id for it in items)))
        start, end = self.section(SECTIONS["phases"])
        if start < 0:
            raise _fail("%s has no `## %s` section" % (self.name, SECTIONS["phases"]))
        heads = [(i, m) for i in range(start, end) if (m := _BLOCK_HEAD_RE.match(self.lines[i]))]
        ids = [it.id.lower() for it in items]
        block_ids = [m.group(1).lower() for _, m in heads]
        if sorted(ids) != sorted(block_ids):
            raise _fail("%s: Progress (%s) and its `### Phase` blocks (%s) disagree; fix that first (`plans validate %s`)"
                        % (self.name, ", ".join(ids), ", ".join(block_ids) or "none", self.name))
        pre = list(self.lines[start:heads[0][0]]) if heads else list(self.lines[start:end])
        blocks = {}
        for n, (i, m) in enumerate(heads):
            stop = heads[n + 1][0] if n + 1 < len(heads) else end
            body = list(self.lines[i:stop])
            while body and not body[-1].strip():
                body.pop()
            blocks[m.group(1).lower()] = (m.group(2), body)
        entries = []
        for it in items:
            btitle, body = blocks[it.id.lower()]
            entries.append({"id": it.id, "box": it.box, "name": it.name, "note": it.note,
                            "btitle": btitle, "block": body})
        return entries, pre

    def _write_phase_layout(self, entries, pre, *, renumber: bool) -> None:
        start, end = self.section(SECTIONS["phases"])
        out = list(pre) if pre else [""]
        for n, e in enumerate(entries, 1):
            if renumber:
                e["id"] = str(n)
            block = list(e["block"])
            block[0] = "### Phase %s: %s" % (e["id"], e["btitle"])
            out += block + [""]
        self.lines[start:end] = out
        items = self.items()
        first, last = items[0].index, items[-1].index
        if last - first + 1 != len(items):
            raise _fail("%s: the Progress list has other lines between its phases; edit the file by hand" % self.name)
        self.lines[first:last + 1] = [
            "- [%s] Phase %s: %s%s" % (e["box"], e["id"], e["name"], _SEP + e["note"] if e["note"] else "")
            for e in entries]
        self.meta["updated"] = _today()

    def _entry_at(self, entries, phase: str) -> int:
        wanted = self.item(phase).id.lower()
        for n, e in enumerate(entries):
            if e["id"].lower() == wanted:
                return n
        raise _fail("%s has no phase %r" % (self.name, phase))

    def add_phase(self, title: str, after: Optional[str] = None) -> str:
        title = " ".join(title.split())
        if not title:
            raise _fail("the phase needs a title")
        entries, pre = self._phase_layout(numeric=True)
        if after in (None, ""):
            at = len(entries)
        elif str(after).strip() == "0":
            at = 0
        else:
            at = self._entry_at(entries, str(after)) + 1
        entries.insert(at, {"id": "0", "box": " ", "name": title, "note": "", "btitle": title,
                            "block": self._template_block(0, title)})
        self._write_phase_layout(entries, pre, renumber=True)
        return entries[at]["id"]

    def rename_phase(self, phase: str, title: str) -> str:
        title = " ".join(title.split())
        if not title:
            raise _fail("the phase needs a title")
        entries, pre = self._phase_layout(numeric=False)
        e = entries[self._entry_at(entries, phase)]
        e["name"] = e["btitle"] = title
        self._write_phase_layout(entries, pre, renumber=False)
        return e["id"]

    def remove_phase(self, phase: str, *, force: bool = False) -> str:
        entries, pre = self._phase_layout(numeric=True)
        n = self._entry_at(entries, phase)
        e = entries[n]
        if e["box"] == "x" and not force:
            raise _fail("Phase %s is checked (%s); pass --force to remove a finished phase" % (e["id"], e["name"]))
        if len(entries) == 1:
            raise _fail("Phase %s is the plan's only phase; a plan needs one" % e["id"])
        del entries[n]
        self._write_phase_layout(entries, pre, renumber=True)
        return "%s: %s" % (e["id"], e["name"])

    def split_phase(self, phase: str, titles: "list[str]") -> "list[str]":
        titles = [" ".join(t.split()) for t in titles]
        if len(titles) < 2 or not all(titles):
            raise _fail("split needs at least two non-empty titles")
        entries, pre = self._phase_layout(numeric=True)
        n = self._entry_at(entries, phase)
        # The first part keeps the box, the outcome and the written block; the others start pending, from the template.
        parts = [dict(entries[n], name=titles[0], btitle=titles[0])]
        for t in titles[1:]:
            parts.append({"id": "0", "box": " ", "name": t, "note": "", "btitle": t,
                          "block": self._template_block(0, t)})
        entries[n:n + 1] = parts
        self._write_phase_layout(entries, pre, renumber=True)
        return [e["id"] for e in parts]

    # -- summaries ------------------------------------------------------------

    def summary(self) -> str:
        done, total = self.counts()
        current = self.current()
        where = " | %s Phase %s: %s" % (
            "active" if current.box == "/" else "next", current.id, current.name
        ) if current else ""
        return "%s  [%s]  %d/%d  %s%s" % (
            self.name, self.status or "?", done, total, self.title, where)

    def as_dict(self) -> "dict[str, Any]":
        done, total = self.counts()
        out = dict(self.meta)
        out.update({
            "name": self.name,
            "path": str(self.path),
            "title": self.title,
            "status": self.status,
            "executor": self.field("executor"),
            "done": done,
            "total": total,
            "is_complex": self.is_complex,
            "extras": [str(p.relative_to(self.path.parent)) for p in self.extras],
            "phases": [{"id": it.id, "name": it.name, "state": it.state, "note": it.note}
                       for it in self.items()],
        })
        return out

    def to_record(self, root: Path) -> Record:
        done, total = self.counts()
        meta = dict(self.meta)
        meta.update({
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "executor": self.field("executor"),
            "done": done,
            "total": total,
        })
        return Record(
            self.path,
            meta,
            "\n".join(self.lines),
            root=root,
            extras=self.extras,
            key_order=_PLAN_KEY_ORDER,
            name=self.name,
        )

    def problems(self) -> "list[str]":
        found: "list[str]" = []
        if not any(line.startswith("# ") for line in self.lines) and not self.meta.get("title"):
            found.append("no `# <title>` line or `title` frontmatter")
        if self.status not in STATUSES:
            found.append("Status %r is not one of: %s" % (self.status, ", ".join(STATUSES)))
        if not self.field("executor"):
            found.append("no `executor` field")
        order = [line[3:].strip() for line in self.lines if line.startswith("## ")]
        required = list(SECTIONS.values())
        if [h for h in order if h in required] != required:
            found.append("sections must be, in order: %s" % ", ".join(required))
        items = self.items()
        if not items:
            found.append("the Progress list is empty")
        start, end = self.section(SECTIONS["phases"])
        blocks = [m.group(1).lower() for m in
                  (re.match(r"^###\s+Phase\s+([0-9A-Za-z.]+)\s*:", line)
                   for line in self.lines[max(start, 0):max(end, 0)]) if m]
        ids = [it.id.lower() for it in items]
        if len(set(ids)) != len(ids):
            found.append("a phase id is used twice in Progress")
        for missing in [i for i in ids if i not in blocks]:
            found.append("Progress lists Phase %s but there is no `### Phase %s:` block"
                         % (missing, missing))
        for extra in [b for b in blocks if b not in ids]:
            found.append("`### Phase %s:` has no line in Progress" % extra)
        left = sorted({m.group(0) for line in _prose_lines(self.lines)
                       for m in _PLACEHOLDER_RE.finditer(line)})
        if left:
            found.append("unfilled placeholders: %s" % ", ".join(left[:6]))
        if "<!--" in "\n".join(self.lines):
            found.append("template comments were not removed")
        return found


class PlansStore:
    """The plans under one directory, arranged as ``plans/<status>/<name>.md``
    (or ``plans/<status>/<name>/README.md`` + ``<phases|extras>.md``)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def status_dir(self, status: str) -> Path:
        folder = STATUS_DIRS.get(status, status)
        return self.root / folder if folder else self.root

    def _scan_base(self, base: Path, *, default_status: str = "", folder_status: str = "") -> "list[Plan]":
        if not base.is_dir():
            return []
        found: "list[Plan]" = []
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.is_file():
                if entry.suffix.lower() != ".md" or entry.name.lower() in _NOT_PLANS:
                    continue
                try:
                    plan = Plan.load(entry, entry.stem, default_status=default_status, folder_status=folder_status)
                except UnicodeDecodeError:
                    _LOGGER.warning("skipping %s: not UTF-8", entry)
                    continue
                found.append(plan)
                # Legacy sub-plans beside flat <name>.md
                sub_dir = base / entry.stem
                if sub_dir.is_dir() and sub_dir.name not in tuple(STATUS_DIRS.values()) + ("reviews",):
                    for sub in sorted(sub_dir.glob("*.md")):
                        if sub.name.lower() in _NOT_PLANS:
                            continue
                        found.append(Plan.load(sub, "%s/%s" % (entry.stem, sub.stem), default_status=plan.status, folder_status=plan.folder_status))
            elif entry.is_dir():
                if entry.name in tuple(STATUS_DIRS.values()) or entry.name in ("reviews", "archive"):
                    continue
                readme = entry / "README.md"
                if readme.is_file():
                    pname = entry.name[:-3] if entry.name.endswith(".md") else entry.name
                    try:
                        plan = Plan.load(readme, pname, default_status=default_status, folder_status=folder_status)
                    except UnicodeDecodeError:
                        _LOGGER.warning("skipping %s: not UTF-8", readme)
                        continue
                    found.append(plan)
                    for sub in sorted(entry.glob("*.md")):
                        if sub.name.lower() in _NOT_PLANS or sub.name.startswith("_"):
                            continue
                        found.append(Plan.load(sub, "%s/%s" % (pname, sub.stem), default_status=plan.status, folder_status=plan.folder_status))
        return found

    def _plans_in_status(self, status: str) -> "list[Plan]":
        d = self.status_dir(status)
        return self._scan_base(d, default_status=status, folder_status=status)

    def live(self) -> "list[Plan]":
        found: "list[Plan]" = []
        for st in LIVE:
            found.extend(self._plans_in_status(st))
        # Also include legacy flat live plans at root during transition
        found.extend(self._scan_base(self.root, default_status="draft"))
        return found

    def on_hold(self) -> "list[Plan]":
        return self._plans_in_status("on-hold")

    def completed(self) -> "list[Plan]":
        return self._plans_in_status("done")

    def all(self) -> "list[Plan]":
        return self.live() + self.on_hold() + self.completed()

    def get(self, name: str) -> Optional[Plan]:
        wanted = name[:-3] if name.endswith(".md") else name
        wanted = wanted.replace("\\", "/").strip("/")
        for plan in self.all():
            if plan.name == wanted:
                return plan
        return None

    def require(self, name: str) -> Plan:
        plan = self.get(name)
        if plan is None:
            raise _fail("no plan %r under %s" % (name, self.root))
        return plan

    def add(
        self,
        name: str,
        *,
        title: str = "",
        executor: str = "",
        phases: "Optional[list[str]]" = None,
        status: str = "draft",
        complex_dir: bool = False,
        attrs: "Optional[dict[str, Any]]" = None,
    ) -> Plan:
        status = status.strip().lower()
        if status not in STATUSES:
            raise _fail("status must be one of: %s" % ", ".join(STATUSES))
        parts = [slugify(p) for p in name.replace("\\", "/").split("/") if p.strip()]
        if not parts or not all(parts) or len(parts) > 2:
            raise _fail("a plan name is `<name>` or `<parent>/<sub>`, in snake_case")
        if parts[-1] in {n[:-3] for n in _NOT_PLANS} or parts[-1] in set(STATUS_DIRS.values()):
            raise _fail("%r is reserved" % parts[-1])
        slug = "/".join(parts)
        if self.get(slug):
            raise _fail("plan %r already exists under %s" % (slug, self.root))

        if len(parts) == 2:
            parent = self.get(parts[0])
            if parent is None:
                raise _fail("sub-plan %r needs its parent plan %r first" % (slug, parts[0]))
            # Promote single-file parent to `<parent>/README.md` if needed
            if not parent.is_complex:
                pdir = parent.path.with_suffix("")
                pdir.mkdir(parents=True, exist_ok=True)
                readme = pdir / "README.md"
                parent.path.replace(readme)
                parent.path = readme
            path = parent.path.parent / (parts[1] + ".md")
            status = parent.status or status
        else:
            dest_dir = self.status_dir(status)
            path = (dest_dir / slug / "README.md") if complex_dir else (dest_dir / (slug + ".md"))

        text = _TEMPLATE.read_text(encoding="utf-8") if _TEMPLATE.is_file() else _FALLBACK_TEMPLATE
        text = _COMMENT_RE.sub("", text).lstrip("\n")
        meta, body, _ = parse_frontmatter(text, typed=True)
        body_lines = body.splitlines()
        legacy, body_lines = _split_legacy_head_fields(body_lines)
        for k, v in legacy.items():
            if v and not meta.get(k):
                meta[k] = v

        plan = Plan(path, body_lines, slug, meta=meta, folder_status=status)
        plan.meta["created"] = _today()
        if title:
            clean_title = " ".join(title.split())
            for i, line in enumerate(plan.lines):
                if line.startswith("# "):
                    plan.lines[i] = "# " + clean_title
                    break
        if executor:
            plan.meta["executor"] = " ".join(executor.split())
        if attrs:
            for k, v in attrs.items():
                if v is None:
                    plan.meta.pop(k, None)
                else:
                    plan.meta[k] = v
        if phases:
            plan.set_phases([" ".join(p.split()) for p in phases])
        path.parent.mkdir(parents=True, exist_ok=True)
        plan.save()
        return plan

    def set_attrs(self, name: str, updates: "dict[str, Any]", *, unset: "Optional[list[str]]" = None) -> Plan:
        plan = self.require(name)
        for k in (unset or []):
            plan.meta.pop(k, None)
        for k, v in updates.items():
            if k.lower() == "status" and v is not None:
                return self.set_status(plan.name, str(v))
            if v is None:
                plan.meta.pop(k, None)
            else:
                plan.meta[k] = v
        plan.meta["updated"] = _today()
        plan.save()
        return plan

    def edit_body(
        self,
        name: str,
        *,
        body: Optional[str] = None,
        section: Optional[str] = None,
        set_text: Optional[str] = None,
        append: Optional[str] = None,
        replace: "Optional[tuple[str, str]]" = None,
        whole: bool = False,
    ) -> Plan:
        """One edit: replace a section (`set_text`), append (to a section, else to the body's end), replace
        exact text (within a section, else the body), or, only with `whole` and no section, replace the body."""
        if body is not None:
            if section:
                raise _fail("--body/--body-file replace the WHOLE plan body and ignore --section; "
                            "to replace one section use `--section %s --set TEXT` (or --set-file)" % section)
            if not whole:
                raise _fail("--body/--body-file replace the WHOLE plan body; pass --whole if that is meant, "
                            "or edit one section with `--section S --set TEXT` / `--append`")
        plan = self.require(name)
        heading = plan.resolve_heading(section) if section else None
        if body is not None:
            plan.lines = body.splitlines()
        elif set_text is not None:
            if heading is None:
                raise _fail("--set replaces a section: pass --section S")
            plan.set_section(heading, set_text)
        elif append is not None:
            if heading is not None:
                plan.append_section(heading, append)
            else:
                plan.lines.extend(["", append.rstrip("\r\n")])
        elif replace is not None:
            start, end = plan.section(heading) if heading else (0, len(plan.lines))
            scope = "\n".join(plan.lines[start:end])
            if replace[0] not in scope:
                raise _fail("%s: %r not found in %s" % (
                    plan.name, replace[0], "section %r" % heading if heading else "body"))
            plan.lines[start:end] = scope.replace(replace[0], replace[1]).splitlines()
        else:
            raise _fail("pass --set, --append or --replace (--whole --body-file replaces everything)")
        plan.meta["updated"] = _today()
        plan.save()
        return plan

    def add_phase(self, name: str, title: str, after: Optional[str] = None) -> "tuple[Plan, str]":
        plan = self.require(name)
        number = plan.add_phase(title, after)
        plan.save()
        return plan, number

    def rename_phase(self, name: str, phase: str, title: str) -> "tuple[Plan, str]":
        plan = self.require(name)
        number = plan.rename_phase(phase, title)
        plan.save()
        return plan, number

    def remove_phase(self, name: str, phase: str, *, force: bool = False) -> "tuple[Plan, str]":
        plan = self.require(name)
        gone = plan.remove_phase(phase, force=force)
        plan.save()
        return plan, gone

    def split_phase(self, name: str, phase: str, titles: "list[str]") -> "tuple[Plan, list[str]]":
        plan = self.require(name)
        ids = plan.split_phase(phase, titles)
        plan.save()
        return plan, ids

    def note_phase(self, name: str, phase: str, text: str) -> "tuple[Plan, Item]":
        plan = self.require(name)
        it = plan.append_phase_note(phase, text)
        plan.save()
        return plan, it

    def set_status(self, name: str, status: str, *, note: str = "", force: bool = False) -> Plan:
        status = status.strip().lower()
        if status not in STATUSES:
            raise _fail("status must be one of: %s" % ", ".join(STATUSES))
        plan = self.require(name)
        note = " ".join(note.split())
        if status == "done":
            open_items = [it for it in plan.items() if it.box != "x"]
            if open_items and not force:
                raise _fail(
                    "%s is not finished: %s. Check them, or pass --force to close it anyway"
                    % (plan.name, ", ".join("Phase %s [%s]" % (it.id, it.state) for it in open_items)))
            if not note:
                raise _fail("closing a plan needs -m: what was done and the evidence")
            plan.set_field("closed", _today())
            plan.set_field("outcome", note)
        elif status == "on-hold":
            if not note:
                raise _fail("parking a plan needs -m: why, and what would restart it")
            plan.set_field("held", "%s %s" % (_today(), note))
        else:
            for key in ("closed", "outcome", "held"):
                plan.set_field(key, None)
        plan.set_field("status", None)
        plan.meta["updated"] = _today()
        plan.save()
        self._move(plan, STATUS_DIRS[status])
        plan.folder_status = status
        plan.save()
        return plan

    def _move(self, plan: Plan, folder: str) -> None:
        """Move a plan (file or ``<name>/`` complex directory) into ``plans/<folder>/``."""
        if "/" in plan.name:
            return
        dest_dir = self.root / folder
        if plan.is_complex:
            src_dir = plan.path.parent
            dest_subdir = dest_dir / src_dir.name
            if dest_subdir == src_dir:
                return
            if dest_subdir.exists():
                raise _fail("cannot move %r: %s already exists" % (plan.name, dest_subdir))
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_dir), str(dest_subdir))
            plan.path = dest_subdir / plan.path.name
            return

        dest = dest_dir / plan.path.name
        if dest == plan.path:
            return
        if dest.exists():
            raise _fail("cannot move %r: %s already exists" % (plan.name, dest))
        sub_src = plan.path.with_suffix("")
        sub_dest = dest.with_suffix("")
        if sub_src.is_dir() and sub_dest.exists():
            raise _fail("cannot move %r: %s already exists" % (plan.name, sub_dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        plan.path.replace(dest)
        if sub_src.is_dir():
            shutil.move(str(sub_src), str(sub_dest))
        plan.path = dest

    def remove(self, name: str, *, force: bool = False) -> Path:
        plan = self.require(name)
        if plan.status == "done" and not force:
            raise _fail("%s is done; pass --force to remove" % plan.name)
        if plan.is_complex:
            target = plan.path.parent
            if plan.extras and not force:
                raise _fail("%s has extra files; pass --force to remove directory" % plan.name)
            shutil.rmtree(target)
            return target
        path = plan.path
        path.unlink()
        return path

    def save(self, plan: Plan) -> None:
        plan.save()

    def query(
        self,
        *,
        sql: Optional[str] = None,
        select: Optional[str] = None,
        where: Optional[str] = None,
        order_by: Optional[str] = None,
        group_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> "tuple[list[str], list[dict[str, Any]]]":
        recs = [p.to_record(self.root) for p in self.all()]
        return query_records(
            recs,
            sql=sql,
            select=select or "name, status, done, total, title",
            where=where,
            order_by=order_by,
            group_by=group_by,
            limit=limit,
            table_alias="plans",
        )

    def migrate(self, *, dry_run: bool = False) -> "dict[str, Any]":
        """Migrate flat plans and legacy body fields to frontmatter + ``plans/<status>/`` and remove ``INDEX.md``."""
        moved: "list[str]" = []
        updated: "list[str]" = []
        removed_index = False
        top_plans = [p for p in self.all() if "/" not in p.name]
        for plan in top_plans:
            st = plan.status if plan.status in STATUS_DIRS else "draft"
            # From here the folder says the status, so the frontmatter stops saying it.
            plan.folder_status = st
            plan.meta.pop("status", None)
            before = plan.path.read_text(encoding="utf-8-sig")
            # Promote legacy `<name>.md` + `<name>/` directory into `<name>/README.md`
            sub_dir = plan.path.with_suffix("")
            needs_complex = not plan.is_complex and sub_dir.is_dir()
            dest_folder = STATUS_DIRS[st]
            target_dir = self.root / dest_folder
            if plan.is_complex or needs_complex:
                target_path = target_dir / plan.name / "README.md"
            else:
                target_path = target_dir / (plan.name + ".md")
            rendered = plan.render()
            if before != rendered or plan.path != target_path:
                if not dry_run:
                    plan.save()
                    if needs_complex:
                        readme = sub_dir / "README.md"
                        plan.path.replace(readme)
                        plan.path = readme
                    if plan.path != target_path:
                        self._move(plan, dest_folder)
                if plan.path != target_path or needs_complex:
                    moved.append("%s -> %s" % (plan.name, target_path))
                else:
                    updated.append(plan.name)
        idx = self.root / INDEX_NAME
        if idx.is_file():
            if not dry_run:
                idx.unlink()
            removed_index = True
        return {"moved": moved, "updated": updated, "removed_index": removed_index}

    def provider_info(self, agents_root: Optional[Path] = None) -> "dict[str, Any]":
        fs = FolderStore(
            self.root,
            status_folders=STATUS_DIRS,
            default_status="draft",
            key_order=_PLAN_KEY_ORDER,
            table_alias="plans",
        )
        return open_provider(fs, agents_root or self.root.parent, scope_key="plans").capabilities()
