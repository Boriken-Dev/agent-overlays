"""Frontmatter-Markdown decisions store backed by ``decisions/<status>/`` folders and ``_records``.

Layout (no ``INDEX.md`` is generated or maintained)::

    decisions/
        proposed/<id_or_slug>.md        or proposed/<name>/README.md + extras
        decided/<id_or_slug>.md         or decided/<name>/README.md + extras
        superseded/<id_or_slug>.md      or superseded/<name>/README.md + extras

Every decision carries YAML frontmatter (``id``, ``name``, ``title``, ``status``,
``description``, ``created``, ``updated``, plus optional ``superseded_by``,
``amended_by``, ``source``, ``owner``, ``tags``, ...), followed by Markdown rationale
and context. Legacy flat ``decisions/D*.md`` files remain readable until
``decisions migrate`` moves them into status folders and removes ``INDEX.md``.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence


def _load_records():
    path = Path(__file__).resolve().parent / "_records.py"
    name = "_records"
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_rec = _load_records()
FolderStore = _rec.FolderStore
open_provider = _rec.open_provider
parse_frontmatter = _rec.parse_frontmatter
render_frontmatter = _rec.render_frontmatter
slugify = _rec.slugify_name
today_iso = _rec.today_iso
write_text_lf = _rec.write_text_lf

_LOGGER = logging.getLogger("decisions")

STATUS_DIRS: "dict[str, str]" = {
    "proposed": "proposed",
    "decided": "decided",
    "superseded": "superseded",
}
_STATUS_ALIASES: "dict[str, str]" = {
    "draft": "proposed",
    "proposed": "proposed",
    "accepted": "decided",
    "active": "decided",
    "decided": "decided",
    "superseded": "superseded",
    "retired": "superseded",
}
_DECISION_KEY_ORDER = (
    "id",
    "name",
    "title",
    "status",
    "description",
    "created",
    "updated",
    "superseded_by",
    "amended_by",
    "owner",
    "source",
    "tags",
)
_IGNORE_FILES = {"index.md", "readme.md", "decisions.md"}
_ID_RE = re.compile(r"^(D\d+)(?:$|[_-])", re.IGNORECASE)
_HEADING_ID_RE = re.compile(r"^#\s+(?:(D\d+)\s*(?:[—–:-]|\s)\s*)?(.+?)\s*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(
    r"(?:^|\b)(?:Status|State)\s*[:\-]\s*(proposed|decided|accepted|superseded|draft|retired)\b",
    re.IGNORECASE,
)
_SUPERSEDED_BY_RE = re.compile(
    r"(?:^|\b)superseded\s+by\s+\[?\[?(D\d+)\]?\]?",
    re.IGNORECASE,
)
_AMENDED_BY_RE = re.compile(
    r"(?:^|\b)amended\s+by\s+\[?\[?(D\d+)\]?\]?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_INDEX_HINT_RE = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*.*?\b(proposed|decided|accepted|superseded)\b",
    re.IGNORECASE,
)


def fail(msg: str) -> SystemExit:
    return SystemExit("error: %s" % (msg,))


def normalize_status(raw: str) -> str:
    key = raw.strip().lower().replace("_", "-")
    if key not in _STATUS_ALIASES:
        raise fail("unknown decision status %r (expected proposed, decided, or superseded)" % (raw,))
    return _STATUS_ALIASES[key]


def _extract_id(name: str, body: str) -> str:
    m = _ID_RE.match(name)
    if m:
        return m.group(1).upper()
    hm = _HEADING_ID_RE.search(body)
    if hm and hm.group(1):
        return hm.group(1).upper()
    return ""


def _extract_title(name: str, body: str) -> str:
    hm = _HEADING_ID_RE.search(body)
    if hm:
        return hm.group(2).strip()
    return name


def _extract_description(meta: "dict[str, Any]", body: str, title: str) -> str:
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    # Look for a bold **Decision:** / **Decision**: line or first non-heading paragraph
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--") or s.startswith("```"):
            continue
        if s.lower().startswith(("date:", "status:", "provenance:", "from finding")):
            continue
        return s[:220]
    return title


def _infer_legacy_status(body: str, folder_status: str, index_hint: str = "") -> str:
    if folder_status in STATUS_DIRS:
        return folder_status
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _STATUS_LINE_RE.search(s)
        if m:
            raw = m.group(1).lower()
            # "Status: decided. Amended by [D<nn>]" stays decided even if body mentions superseded elsewhere
            return _STATUS_ALIASES.get(raw, "decided")
    if index_hint and index_hint.lower() in _STATUS_ALIASES:
        return _STATUS_ALIASES[index_hint.lower()]
    return "decided"


@dataclass
class Decision:
    name: str
    path: Path
    meta: "dict[str, Any]"
    body: str
    folder_status: str = ""
    is_complex: bool = False
    extras: "list[Path]" = field(default_factory=list)

    @property
    def id(self) -> str:
        val = self.meta.get("id")
        if isinstance(val, str) and val.strip():
            return val.strip().upper()
        return _extract_id(self.name, self.body) or self.name

    @property
    def status(self) -> str:
        if self.folder_status in STATUS_DIRS:
            return self.folder_status
        raw = str(self.meta.get("status") or self.folder_status or "decided").strip().lower()
        return _STATUS_ALIASES.get(raw, raw)

    def _derived(self) -> "dict[str, bool]":
        """Which fields the path or the body already says, so the frontmatter must not repeat them."""
        return {
            "name": True,
            "id": bool(_ID_RE.match(self.name)),
            "title": bool(_HEADING_ID_RE.search(self.body)),
            "status": self.folder_status in STATUS_DIRS,
        }

    @property
    def title(self) -> str:
        val = self.meta.get("title")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return _extract_title(self.name, self.body)

    @property
    def description(self) -> str:
        return _extract_description(self.meta, self.body, self.title)

    @classmethod
    def load(
        cls,
        path: Path,
        name: Optional[str] = None,
        *,
        folder_status: str = "",
        is_complex: bool = False,
        extras: "Sequence[Path]" = (),
        index_hint: str = "",
    ) -> "Decision":
        raw = path.read_text(encoding="utf-8-sig")
        meta, body, _ = parse_frontmatter(raw, typed=True)
        stem = name or (path.parent.name if is_complex else path.stem)
        meta.pop("name", None)
        title = _extract_title(stem, body)
        if folder_status in STATUS_DIRS:
            meta.pop("status", None)
        else:
            st = meta.get("status")
            if isinstance(st, str) and st.strip():
                meta["status"] = _STATUS_ALIASES.get(st.strip().lower(), st.strip().lower())
            else:
                meta["status"] = _infer_legacy_status(body, folder_status, index_hint=index_hint)
        desc = _extract_description(meta, body, title)
        if desc:
            meta.setdefault("description", desc)
        if "created" not in meta:
            dm = _DATE_RE.search(body[:1200])
            meta["created"] = dm.group(1) if dm else today_iso()
        if "updated" not in meta:
            meta["updated"] = str(meta["created"])
        if "superseded_by" not in meta:
            sm = _SUPERSEDED_BY_RE.search(body[:1500])
            if sm and (folder_status or meta.get("status")) == "superseded":
                meta["superseded_by"] = sm.group(1).upper()
        if "amended_by" not in meta:
            am = _AMENDED_BY_RE.search(body[:1500])
            if am:
                meta["amended_by"] = am.group(1).upper()
        return cls(
            name=stem,
            path=path,
            meta=meta,
            body=body.lstrip("\n"),
            folder_status=folder_status,
            is_complex=is_complex,
            extras=sorted(extras),
        )

    def render(self) -> str:
        meta = dict(self.meta)
        derived = self._derived()
        for key, said in derived.items():
            if said:
                meta.pop(key, None)
        if not derived["id"] and self.id and "id" not in meta:
            meta["id"] = self.id
        if not derived["title"]:
            meta.setdefault("title", self.title)
        return render_frontmatter(meta, self.body.lstrip("\n"), key_order=_DECISION_KEY_ORDER)

    def save(self) -> None:
        write_text_lf(self.path, self.render())

    def as_dict(self) -> "dict[str, Any]":
        data: "dict[str, Any]" = {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "description": self.description,
            "path": str(self.path),
            "complex": self.is_complex,
            "extras": [p.name for p in self.extras],
        }
        for k, v in self.meta.items():
            data.setdefault(k, v)
        return data


class DecisionsStore:
    """Folder store over ``decisions/<status>/`` with legacy flat file support."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def status_dir(self, status: str) -> Path:
        norm = normalize_status(status)
        return self.root / STATUS_DIRS[norm]

    def _scan_dir(
        self,
        directory: Path,
        folder_status: str,
        index_hints: "dict[str, str]",
    ) -> "list[Decision]":
        if not directory.is_dir():
            return []
        found: "list[Decision]" = []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".md":
                if entry.name.lower() in _IGNORE_FILES:
                    continue
                hint = index_hints.get(entry.name, "") or index_hints.get(entry.stem, "")
                try:
                    found.append(Decision.load(entry, entry.stem, folder_status=folder_status, index_hint=hint))
                except UnicodeDecodeError:
                    _LOGGER.warning("skipping %s: not UTF-8", entry)
            elif entry.is_dir():
                if entry.name in STATUS_DIRS.values() and not folder_status:
                    continue
                readme = entry / "README.md"
                if readme.is_file():
                    extras = [p for p in sorted(entry.rglob("*.md")) if p != readme]
                    hint = index_hints.get(entry.name, "")
                    try:
                        found.append(
                            Decision.load(
                                readme,
                                entry.name,
                                folder_status=folder_status,
                                is_complex=True,
                                extras=extras,
                                index_hint=hint,
                            )
                        )
                    except UnicodeDecodeError:
                        _LOGGER.warning("skipping %s: not UTF-8", readme)
        return found

    def _index_hints(self) -> "dict[str, str]":
        idx = self.root / "INDEX.md"
        if not idx.is_file():
            return {}
        hints: "dict[str, str]" = {}
        try:
            text = idx.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return {}
        for line in text.splitlines():
            m = _INDEX_HINT_RE.match(line)
            if m:
                label, target, status = m.group(1).strip(), m.group(2).strip(), m.group(3).lower()
                hints[Path(target).name] = status
                hints[Path(target).stem] = status
                hints[label.upper()] = status
        return hints

    def all(self) -> "list[Decision]":
        hints = self._index_hints()
        seen: "dict[str, Decision]" = {}
        for st in ("decided", "proposed", "superseded"):
            for dec in self._scan_dir(self.root / STATUS_DIRS[st], st, hints):
                seen[dec.name] = dec
        for dec in self._scan_dir(self.root, "", hints):
            seen.setdefault(dec.name, dec)
        return sorted(seen.values(), key=lambda d: (self._id_sort_key(d.id), d.name.lower()))

    @staticmethod
    def _id_sort_key(did: str) -> "tuple[int, str]":
        m = re.match(r"^D(\d+)$", did.strip(), re.IGNORECASE)
        if m:
            return (int(m.group(1)), did.upper())
        return (10**9, did.upper())

    def get(self, key: str) -> Optional[Decision]:
        needle = key.strip().removesuffix(".md")
        upper = needle.upper()
        # 1. Exact stem match
        for d in self.all():
            if d.name == needle or d.name.lower() == needle.lower():
                return d
        # 2. Match by ID (`D<nn>`, e.g. uppercase/lowercase match)
        for d in self.all():
            if d.id.upper() == upper:
                return d
        return None

    def require(self, key: str) -> Decision:
        dec = self.get(key)
        if dec is None:
            raise fail("no decision %r under %s" % (key, self.root))
        return dec

    def next_id(self) -> str:
        highest = 0
        width = 2
        for d in self.all():
            m = re.match(r"^D(\d+)$", d.id, re.IGNORECASE)
            if m:
                num = int(m.group(1))
                width = max(width, len(m.group(1)))
                if num > highest:
                    highest = num
        return "D%s" % str(highest + 1).zfill(width)

    def add(
        self,
        title: str,
        *,
        decision_id: str = "",
        name: Optional[str] = None,
        description: str = "",
        status: str = "decided",
        provenance: str = "",
        body: str = "",
        attrs: "Optional[dict[str, Any]]" = None,
        complex_dir: bool = False,
    ) -> Decision:
        clean_title = title.strip()
        if not clean_title:
            raise fail("a decision needs a non-empty title")
        norm_status = normalize_status(status)
        did = decision_id.strip().upper() if decision_id.strip() else self.next_id()
        if not re.match(r"^D\d+$", did):
            raise fail("decision id must match D<digits>, got %r" % (did,))
        if self.get(did) is not None:
            raise fail("decision id %r already exists under %s" % (did, self.root))

        if name and name.strip():
            stem = name.strip().removesuffix(".md")
        else:
            slug = slugify(clean_title, sep="_") or "decision"
            stem = "%s_%s" % (did, slug)
        if stem.lower() in {"index", "readme"}:
            raise fail("decision name %r is reserved" % (stem,))
        if self.get(stem) is not None:
            raise fail("decision %r already exists under %s" % (stem, self.root))

        desc = description.strip() or clean_title
        today = today_iso()
        meta: "dict[str, Any]" = {
            "id": did,
            "name": stem,
            "title": clean_title,
            "status": norm_status,
            "description": desc,
            "created": today,
            "updated": today,
        }
        if provenance.strip():
            meta["provenance"] = provenance.strip()
        if attrs:
            meta.update(attrs)

        if body.strip():
            doc_body = body.strip() + "\n"
        else:
            lines = ["# %s — %s" % (did, clean_title), ""]
            if provenance.strip():
                lines.extend(["Provenance: %s" % provenance.strip(), ""])
            lines.extend(["**Decision:** %s" % desc, ""])
            doc_body = "\n".join(lines)

        dest_dir = self.status_dir(norm_status)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if complex_dir:
            path = dest_dir / stem / "README.md"
        else:
            path = dest_dir / (stem + ".md")
        dec = Decision(name=stem, path=path, meta=meta, body=doc_body, folder_status=norm_status, is_complex=complex_dir)
        dec.save()
        return dec

    def set_attrs(
        self,
        key: str,
        updates: "dict[str, Any]",
        *,
        unset: "Sequence[str]" = (),
    ) -> Decision:
        dec = self.require(key)
        for k, v in updates.items():
            if k == "status":
                # The status is the folder: changing it moves the decision.
                self.set_status(dec.name, str(v))
                dec = self.require(dec.name)
            else:
                dec.meta[k] = v
        for k in unset:
            dec.meta.pop(k, None)
        dec.meta["updated"] = today_iso()
        dec.save()
        return dec

    def edit_body(
        self,
        key: str,
        *,
        body: Optional[str] = None,
        section: Optional[str] = None,
        append: Optional[str] = None,
        replace: "Optional[tuple[str, str]]" = None,
    ) -> Decision:
        dec = self.require(key)
        fs_rec = _rec.Record(
            dec.path,
            dec.meta,
            dec.body,
            root=self.root,
            extras=list(dec.extras),
            key_order=_DECISION_KEY_ORDER,
        )
        if body is not None:
            fs_rec.body = body.rstrip("\r\n") + "\n"
        elif section is not None:
            sec_text = fs_rec.section_text(section)
            if append is not None:
                fs_rec.append_to_section(section, append)
            elif replace is not None:
                if sec_text is None:
                    raise fail("%s: section %r not found" % (dec.name, section))
                old, new = replace
                if old not in sec_text:
                    raise fail("%s: %r not found in section %r" % (dec.name, old, section))
                inner = sec_text.split("\n", 1)[1] if "\n" in sec_text else ""
                fs_rec.set_section(section, inner.replace(old, new))
            else:
                raise fail("pass --append or --replace with --section")
        elif append is not None:
            fs_rec.body = fs_rec.body.rstrip("\r\n") + "\n\n" + append.strip("\r\n") + "\n"
        elif replace is not None:
            fs_rec.replace_in_body(replace[0], replace[1])
        else:
            raise fail("pass --body, --append, or --replace")
        dec.body = fs_rec.body
        dec.meta["updated"] = today_iso()
        dec.save()
        return dec

    def _move_to_status(self, dec: Decision, new_status: str) -> Path:
        norm = normalize_status(new_status)
        dest_dir = self.status_dir(norm)
        if dec.is_complex:
            src_dir = dec.path.parent
            dest_subdir = dest_dir / src_dir.name
            if dest_subdir != src_dir:
                legacy_dir = self.root / src_dir.name
                if dest_subdir.exists() or (norm == "decided" and legacy_dir != src_dir and legacy_dir.exists()):
                    existing = dest_subdir if dest_subdir.exists() else legacy_dir
                    raise fail("cannot move %r: %s already exists" % (dec.name, existing))
                dest_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(src_dir), str(dest_subdir))
                dec.path = dest_subdir / dec.path.name
        else:
            dest_file = dest_dir / dec.path.name
            if dest_file != dec.path:
                legacy_file = self.root / dec.path.name
                if dest_file.exists() or (norm == "decided" and legacy_file != dec.path and legacy_file.exists()):
                    existing = dest_file if dest_file.exists() else legacy_file
                    raise fail("cannot move %r: %s already exists" % (dec.name, existing))
                dest_dir.mkdir(parents=True, exist_ok=True)
                dec.path.replace(dest_file)
                dec.path = dest_file
        dec.folder_status = norm
        return dec.path

    def set_status(
        self,
        key: str,
        new_status: str,
        *,
        by: str = "",
        note: str = "",
    ) -> Decision:
        dec = self.require(key)
        norm = normalize_status(new_status)
        if norm == "superseded" and not (by.strip() or dec.meta.get("superseded_by")):
            raise fail("superseding a decision requires --by <Dnn> naming its replacement")
        dec.meta["updated"] = today_iso()
        if by.strip():
            dec.meta["superseded_by"] = by.strip().upper()
        if note.strip():
            heading = "## Superseded (%s)" % today_iso() if norm == "superseded" else "## Note (%s)" % today_iso()
            line = "%s\n\n%s\n" % (heading, note.strip())
            if by.strip() and by.strip().upper() not in note.upper():
                line = "%s\n\nSuperseded by %s. %s\n" % (heading, by.strip().upper(), note.strip())
            dec.body = dec.body.rstrip("\r\n") + "\n\n" + line
        elif norm == "superseded" and by.strip():
            dec.body = dec.body.rstrip("\r\n") + "\n\n## Superseded (%s)\n\nSuperseded by %s.\n" % (
                today_iso(),
                by.strip().upper(),
            )
        dec.save()
        self._move_to_status(dec, norm)
        dec.meta.pop("status", None)
        dec.save()
        return dec

    def remove(self, key: str, *, force: bool = False) -> Path:
        dec = self.require(key)
        if dec.status in ("decided", "superseded") and not force:
            raise fail(
                "decision %r has status %r; settled decisions are kept (supersede it or pass --force)"
                % (dec.name, dec.status)
            )
        if dec.is_complex:
            import shutil
            target = dec.path.parent
            if dec.extras and not force:
                raise fail("%s has extra files; pass --force to remove directory" % (dec.name,))
            shutil.rmtree(target)
            return target
        path = dec.path
        path.unlink()
        return path

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
        recs = [
            _rec.Record(
                d.path,
                dict(d.meta, id=d.id, title=d.title, status=d.status),
                d.body,
                root=self.root,
                extras=list(d.extras),
                key_order=_DECISION_KEY_ORDER,
                name=d.name,
            )
            for d in self.all()
        ]
        return _rec.query_records(
            recs,
            sql=sql,
            select=select or "id, status, title, description",
            where=where,
            order_by=order_by,
            group_by=group_by,
            limit=limit,
            table_alias="decisions",
        )

    def migrate(self, *, dry_run: bool = False) -> "dict[str, Any]":
        moved: "list[str]" = []
        updated: "list[str]" = []
        removed_index = False
        for dec in self.all():
            norm = normalize_status(dec.status)
            dec.folder_status = norm
            dec.meta.pop("status", None)
            before = dec.path.read_text(encoding="utf-8-sig")
            dest_dir = self.status_dir(norm)
            target_path = (dest_dir / dec.name / "README.md") if dec.is_complex else (dest_dir / dec.path.name)
            rendered = dec.render()
            if before != rendered or dec.path != target_path:
                if not dry_run:
                    dec.save()
                    if dec.path != target_path:
                        self._move_to_status(dec, norm)
                if dec.path != target_path:
                    moved.append("%s -> %s" % (dec.name, target_path))
                else:
                    updated.append(dec.name)
        idx = self.root / "INDEX.md"
        if idx.is_file():
            if not dry_run:
                idx.unlink()
            removed_index = True
        return {"moved": moved, "updated": updated, "removed_index": removed_index}

    def provider_info(self, agents_root: Optional[Path] = None) -> "dict[str, Any]":
        fs = FolderStore(
            self.root,
            status_folders=STATUS_DIRS,
            default_status="decided",
            key_order=_DECISION_KEY_ORDER,
            table_alias="decisions",
        )
        return open_provider(fs, agents_root or self.root.parent, scope_key="decisions").capabilities()
