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
    ):
        self.path = Path(path)
        self.lines = list(lines)
        self.name = name
        self.meta: "dict[str, Any]" = dict(meta or {})
        self.extras: "list[Path]" = list(extras or [])

    @property
    def is_complex(self) -> bool:
        return self.path.name.lower() == "readme.md"

    @classmethod
    def load(cls, path: Path, name: str, *, default_status: str = "") -> "Plan":
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        meta, body, had_fm = parse_frontmatter(text, typed=True)
        body_lines = body.splitlines()
        legacy, cleaned_lines = _split_legacy_head_fields(body_lines)
        for k, v in legacy.items():
            if v and not meta.get(k):
                meta[k] = v
        if not meta.get("name"):
            meta["name"] = name
        if not meta.get("status") and default_status:
            meta["status"] = default_status
        if not meta.get("title"):
            for line in cleaned_lines:
                if line.startswith("# "):
                    meta["title"] = line[2:].strip()
                    break
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
        return cls(path, body_lines, name, meta=meta, extras=extras)

    def render(self) -> str:
        meta = dict(self.meta)
        meta.setdefault("name", self.name)
        meta.setdefault("title", self.title)
        if self.status:
            meta["status"] = self.status
        _, cleaned = _split_legacy_head_fields(self.lines)
        body = "\n".join(cleaned).rstrip("\n") + "\n"
        return render_frontmatter(meta, body, key_order=_PLAN_KEY_ORDER)

    def save(self) -> None:
        _, self.lines = _split_legacy_head_fields(self.lines)
        if not self.meta.get("title"):
            self.meta["title"] = self.title
        self.meta["name"] = self.name
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
            blocks += ["### Phase %d: %s" % (n, p), "", "- Files: <paths>",
                       "- Guidance: <signatures, hints, defaults>", "- Why: <one line>",
                       "- Done-when: <observable check>", ""]
        self.lines[start:end] = blocks

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

    def _scan_base(self, base: Path, *, default_status: str = "") -> "list[Plan]":
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
                    plan = Plan.load(entry, entry.stem, default_status=default_status)
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
                        found.append(Plan.load(sub, "%s/%s" % (entry.stem, sub.stem), default_status=plan.status))
            elif entry.is_dir():
                if entry.name in tuple(STATUS_DIRS.values()) or entry.name in ("reviews", "archive"):
                    continue
                readme = entry / "README.md"
                if readme.is_file():
                    pname = entry.name[:-3] if entry.name.endswith(".md") else entry.name
                    try:
                        plan = Plan.load(readme, pname, default_status=default_status)
                    except UnicodeDecodeError:
                        _LOGGER.warning("skipping %s: not UTF-8", readme)
                        continue
                    found.append(plan)
                    for sub in sorted(entry.glob("*.md")):
                        if sub.name.lower() in _NOT_PLANS or sub.name.startswith("_"):
                            continue
                        found.append(Plan.load(sub, "%s/%s" % (pname, sub.stem), default_status=plan.status))
        return found

    def _plans_in_status(self, status: str) -> "list[Plan]":
        d = self.status_dir(status)
        return self._scan_base(d, default_status=status)

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

        plan = Plan(path, body_lines, slug, meta=meta)
        plan.meta["name"] = slug
        plan.meta["status"] = status
        plan.meta["created"] = _today()
        if title:
            clean_title = " ".join(title.split())
            plan.meta["title"] = clean_title
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
        append: Optional[str] = None,
        replace: "Optional[tuple[str, str]]" = None,
    ) -> Plan:
        plan = self.require(name)
        if body is not None:
            plan.lines = body.splitlines()
        elif section and append is not None:
            heading = SECTIONS.get(section.lower(), section)
            start, end = plan.section(heading)
            if start < 0:
                if plan.lines and plan.lines[-1].strip():
                    plan.lines.append("")
                plan.lines.extend(["## " + heading, "", append.rstrip("\r\n"), ""])
            else:
                at = end
                while at > start and not plan.lines[at - 1].strip():
                    at -= 1
                plan.lines.insert(at, append.rstrip("\r\n"))
        elif append is not None:
            plan.lines.extend(["", append.rstrip("\r\n")])
        elif replace is not None:
            full = "\n".join(plan.lines)
            if replace[0] not in full:
                raise _fail("%s: %r not found in body" % (plan.name, replace[0]))
            plan.lines = full.replace(replace[0], replace[1]).splitlines()
        else:
            raise _fail("pass --body, --append, or --replace")
        plan.meta["updated"] = _today()
        plan.save()
        return plan

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
        plan.set_field("status", status)
        plan.meta["updated"] = _today()
        plan.save()
        self._move(plan, STATUS_DIRS[status])
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
            if plan.meta.get("status") != st:
                plan.meta["status"] = st
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
