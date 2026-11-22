"""Shared frontmatter-Markdown record store, SQLite query engine, and pluggable providers.

Every record is a Markdown file with YAML frontmatter for structured fields/attributes
and Markdown context after the frontmatter:

- Simple record: ``<root>/<status>/<name>.md`` (or ``<root>/<name>.md`` when flat)
- Complex record: ``<root>/<status>/<name>/README.md`` + sibling ``<phase|extra>.md``

No ``INDEX.md`` is written or maintained. Queries run through an in-memory,
read-only SQLite table built on demand from the folder's frontmatter and bodies.

Standard library only; Python 3.9 or later.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

_LOGGER = logging.getLogger("engineering.records")

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_EMPTY_FRONTMATTER_RE = re.compile(r"\A---\r?\n---[ \t]*(?:\r?\n|\Z)")
_YAML_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
_YAML_WORDS = frozenset(("true", "false", "yes", "no", "on", "off", "null", "~"))
_YAML_BREAKS = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}

_IGNORED_NAMES = frozenset(("index.md",))
_NON_RECORD_DIRS = frozenset((".git", "__pycache__", "reviews", "archive"))


def fail(message: str) -> SystemExit:
    """Standard CLI error exit."""
    return SystemExit("error: " + message)


def today_iso() -> str:
    """Today's date in ISO-8601 format."""
    return datetime.date.today().isoformat()


def _default_file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def write_text_lf(path: "str | os.PathLike[str]", text: str, *, atomic: bool = False) -> Path:
    """Write ``text`` as UTF-8 with LF line endings (Python 3.9+ safe)."""
    given = Path(path)
    if not atomic:
        given.parent.mkdir(parents=True, exist_ok=True)
        with open(given, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return given
    target = Path(os.path.realpath(str(given)))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if target.exists():
            shutil.copymode(str(target), tmp_name)
        elif os.name != "nt":
            os.chmod(tmp_name, _default_file_mode())
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return given


def yaml_scalar(value: Any) -> str:
    """Render a scalar value as safe YAML (bare when unambiguous, else JSON-quoted)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(yaml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    plain = (
        text == text.strip()
        and text[:1] not in _YAML_INDICATORS
        and ": " not in text
        and " #" not in text
        and not text.endswith(":")
        and text.lower() not in _YAML_WORDS
        and not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text)
        and not any(ord(c) < 32 or c == "\x7f" or c in _YAML_BREAKS for c in text)
    )
    if plain and text:
        return text
    quoted = json.dumps(text, ensure_ascii=False)
    for char, escape in _YAML_BREAKS.items():
        quoted = quoted.replace(char, escape)
    return quoted


def yaml_value(raw: str, *, typed: bool = False) -> Any:
    """Parse a YAML frontmatter scalar or inline list."""
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, str) or typed else raw
        except ValueError:
            return raw
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        inner = raw[1:-1]
        if "'" not in inner.replace("''", ""):
            return inner.replace("''", "'")
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        items = []
        for part in inner.split(","):
            part = part.strip()
            if part:
                items.append(yaml_value(part, typed=typed))
        return items
    if typed:
        low = raw.lower()
        if low in ("null", "~"):
            return None
        if low == "true":
            return True
        if low == "false":
            return False
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", raw):
            return int(raw)
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\.[0-9]+", raw):
            return float(raw)
    return raw


def parse_frontmatter(text: str, *, typed: bool = False) -> "tuple[dict[str, Any], str, bool]":
    """Parse YAML frontmatter from ``text`` -> ``(meta, body, had_frontmatter)``."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        if _EMPTY_FRONTMATTER_RE.match(text):
            end = _EMPTY_FRONTMATTER_RE.match(text).end()  # type: ignore[union-attr]
            return {}, text[end:].lstrip("\r\n"), True
        return {}, text, False
    meta: "dict[str, Any]" = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        key, sep, rest = line.partition(":")
        if not sep or not key.strip():
            i += 1
            continue
        k = key.strip()
        val_raw = rest.strip()
        if not val_raw:
            # Check for indented block list (`  - item` or `- item`)
            items: "list[Any]" = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                nxt_stripped = nxt.strip()
                if not nxt_stripped:
                    j += 1
                    continue
                if re.match(r"^\s*-\s+", nxt):
                    item_raw = re.sub(r"^\s*-\s+", "", nxt, count=1)
                    items.append(yaml_value(item_raw, typed=typed))
                    j += 1
                    continue
                break
            if items:
                meta[k] = items
                i = j
                continue
            meta[k] = ""
        else:
            meta[k] = yaml_value(val_raw, typed=typed)
        i += 1
    body = text[m.end():].lstrip("\r\n")
    return meta, body, True


def render_frontmatter(
    meta: "dict[str, Any]",
    body: str,
    *,
    key_order: "tuple[str, ...]" = (),
) -> str:
    """Render ``meta`` as YAML frontmatter followed by ``body`` (LF-only)."""
    lines = ["---"]
    written: "set[str]" = set()

    def emit(key: str, value: Any) -> None:
        if value is None or value == "" or value == []:
            return
        if isinstance(value, list) and any(isinstance(v, str) and len(v) > 40 for v in value):
            lines.append("%s:" % key)
            for item in value:
                lines.append("  - %s" % yaml_scalar(item))
        else:
            lines.append("%s: %s" % (key, yaml_scalar(value)))
        written.add(key)

    for key in key_order:
        if key in meta:
            emit(key, meta[key])
    for key, value in meta.items():
        if key not in written:
            emit(key, value)
    lines.append("---")
    clean_body = body.lstrip("\r\n").rstrip("\r\n")
    return "\n".join(lines) + "\n\n" + (clean_body + "\n" if clean_body else "")


def requote_frontmatter(text: str) -> str:
    """Requote bare frontmatter scalars that YAML would misread, keeping body intact."""
    shift = 1 if text.startswith("\ufeff") else 0
    m = _FRONTMATTER_RE.match(text[shift:])
    if not m:
        return text
    start, end = m.start(1) + shift, m.end(1) + shift
    out = []
    for line in text[start:end].splitlines(True):
        ln = line.rstrip("\r\n")
        key, sep, value = ln.partition(":")
        bare = value.strip()
        if sep and key.strip() and bare and yaml_value(bare) == bare and yaml_scalar(bare) != bare:
            ln = "%s: %s" % (key, yaml_scalar(bare))
        out.append(ln + line[len(line.rstrip("\r\n")):])
    block = "".join(out)
    return text[:start] + block + text[end:] if block != text[start:end] else text


def coerce_attr_value(raw: str) -> Any:
    """Parse CLI ``key=value`` strings (lists, ints, bools, null, or text)."""
    stripped = raw.strip()
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if stripped.lower() == "null":
        return None
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", stripped):
        return int(stripped)
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\.[0-9]+", stripped):
        return float(stripped)
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
        inner = stripped[1:-1].strip()
        return [p.strip() for p in inner.split(",") if p.strip()] if inner else []
    return raw


def slugify_name(text: str, *, sep: str = "-", max_len: int = 60) -> str:
    """Derive a safe filename slug from free text."""
    pat = r"[^a-z0-9]+"
    slug = re.sub(pat, sep, text.lower()).strip(sep)
    if max_len > 0 and len(slug) > max_len:
        slug = slug[:max_len].rstrip(sep)
    return slug


class Record:
    """One Markdown record with YAML frontmatter and optional sibling files."""

    def __init__(
        self,
        path: Path,
        meta: "dict[str, Any]",
        body: str,
        *,
        root: Optional[Path] = None,
        extras: "Optional[list[Path]]" = None,
        key_order: "tuple[str, ...]" = (),
        status_folders: "Optional[dict[str, str]]" = None,
        name: Optional[str] = None,
    ):
        self.path = Path(path)
        # A caller that names records differently from their files (a sub-plan, `parent/sub`) passes the name.
        self._name = name
        self.meta = dict(meta)
        self.body = body
        self.root = Path(root) if root else None
        self.extras = list(extras) if extras else []
        self.key_order = key_order
        self.status_folders = dict(status_folders) if status_folders else None
        # The name is the file's (or a complex record's folder's) and the status the folder it sits in: neither is
        # kept in the frontmatter, so there is one place to change each.
        self.meta.pop("name", None)
        if self.status_folders:
            self.meta.pop("status", None)

    @property
    def is_complex(self) -> bool:
        """True when the record is a directory's ``README.md``."""
        return self.path.name.lower() == "readme.md"

    @property
    def container_dir(self) -> Path:
        """Directory holding a complex record (or parent directory of a file record)."""
        return self.path.parent

    @property
    def name(self) -> str:
        if self._name:
            return self._name
        if self.is_complex:
            parent_name = self.path.parent.name
            return parent_name[:-3] if parent_name.endswith(".md") else parent_name
        return self.path.stem

    @property
    def status(self) -> str:
        if self.status_folders:
            folder = (self.path.parent.parent if self.is_complex else self.path.parent).name
            for status, status_folder in self.status_folders.items():
                if status_folder and status_folder == folder:
                    return status
            # A record at the store's root is in the status whose folder is the root itself, if any.
            return next((s for s, f in self.status_folders.items() if not f), "")
        return str(self.meta.get("status", ""))

    @property
    def title(self) -> str:
        if self.meta.get("title"):
            return str(self.meta["title"])
        for line in self.body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return self.name

    @property
    def description(self) -> str:
        if self.meta.get("description"):
            return str(self.meta["description"])
        for line in self.body.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s[:120]
        return self.title

    @property
    def folder(self) -> str:
        if self.root is None:
            return self.path.parent.name
        try:
            rel = self.path.parent.relative_to(self.root)
            return "" if str(rel) == "." else rel.as_posix()
        except ValueError:
            return self.path.parent.name

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        root: Optional[Path] = None,
        status_folders: "Optional[dict[str, str]]" = None,
        typed: bool = True,
        key_order: "tuple[str, ...]" = (),
    ) -> "Record":
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        meta, body, _ = parse_frontmatter(text, typed=typed)
        is_complex = path.name.lower() == "readme.md"
        extras: "list[Path]" = []
        if is_complex:
            for item in sorted(path.parent.rglob("*.md")):
                if item == path or item.name.lower() in _IGNORED_NAMES:
                    continue
                if any(part in _NON_RECORD_DIRS for part in item.relative_to(path.parent).parts):
                    continue
                extras.append(item)
        return cls(path, meta, body, root=root, extras=extras, key_order=key_order, status_folders=status_folders)

    def render(self) -> str:
        return render_frontmatter(self.meta, self.body, key_order=self.key_order)

    def save(self, *, atomic: bool = False) -> None:
        write_text_lf(self.path, self.render(), atomic=atomic)

    def section_range(self, heading: str) -> "tuple[int, int]":
        """Line indices ``(after_heading, next_h2)`` in ``self.body.splitlines()``."""
        lines = self.body.splitlines()
        start = -1
        for i, line in enumerate(lines):
            if line.startswith("## "):
                if start >= 0:
                    return start, i
                if line[3:].strip().lower() == heading.strip().lower():
                    start = i + 1
        return (start, len(lines)) if start >= 0 else (-1, -1)

    def section_text(self, heading: str) -> str:
        lines = self.body.splitlines()
        start, end = self.section_range(heading)
        if start < 0:
            raise fail("%s has no `## %s` section" % (self.name, heading))
        header = lines[start - 1]
        return "\n".join([header] + lines[start:end]).rstrip() + "\n"

    def set_section(self, heading: str, content: str) -> None:
        lines = self.body.splitlines()
        start, end = self.section_range(heading)
        new_lines = [""] + content.strip("\r\n").splitlines() + [""]
        if start < 0:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["## " + heading] + new_lines)
        else:
            lines[start:end] = new_lines
        self.body = "\n".join(lines).rstrip("\n") + "\n"

    def append_to_section(self, heading: str, text: str) -> None:
        lines = self.body.splitlines()
        start, end = self.section_range(heading)
        if start < 0:
            self.set_section(heading, text)
            return
        at = end
        while at > start and not lines[at - 1].strip():
            at -= 1
        lines.insert(at, text.rstrip("\r\n"))
        self.body = "\n".join(lines).rstrip("\n") + "\n"

    def replace_in_body(self, old: str, new: str) -> int:
        count = self.body.count(old)
        if count == 0:
            raise fail("%s: %r not found in body" % (self.name, old))
        self.body = self.body.replace(old, new)
        return count

    def as_dict(self, *, include_body: bool = True) -> "dict[str, Any]":
        out: "dict[str, Any]" = dict(self.meta)
        out["name"] = self.name
        out["status"] = self.status
        out["path"] = str(self.path)
        out["folder"] = self.folder
        out["is_complex"] = self.is_complex
        out["extras"] = [str(p.relative_to(self.path.parent)) for p in self.extras]
        if include_body:
            out["body"] = self.body
        return out


# --------------------------------------------------------------------------- #
# SQL-like query engine over folder records (read-only SQLite :memory:)
# --------------------------------------------------------------------------- #

_SAFE_COL_RE = re.compile(r"[^a-zA-Z0-9_]")
_SQL_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}


def _sql_col(name: str) -> str:
    col = _SAFE_COL_RE.sub("_", name.strip().lower()).strip("_")
    if not col or col[0].isdigit():
        col = "col_" + col
    return col


def _sql_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


def _authorizer(action, arg1, arg2, db_name, trigger_name):
    if action in _SQL_ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() == "query_only":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _build_query_db(
    records: "list[Record]",
    *,
    table_alias: Optional[str] = None,
    extra_fields: "Optional[callable]" = None,
) -> "tuple[sqlite3.Connection, list[str]]":
    base_cols = [
        "name", "status", "folder", "path", "is_complex", "extras_count",
        "title", "description", "created", "updated", "body",
    ]
    rows_data: "list[dict[str, Any]]" = []
    seen_cols: "dict[str, None]" = {c: None for c in base_cols}
    for rec in records:
        row: "dict[str, Any]" = {
            "name": rec.name,
            "status": rec.status,
            "folder": rec.folder,
            "path": str(rec.path),
            "is_complex": 1 if rec.is_complex else 0,
            "extras_count": len(rec.extras),
            "title": rec.title,
            "description": rec.description,
            "created": rec.meta.get("created", ""),
            "updated": rec.meta.get("updated", ""),
            "body": rec.body,
        }
        for k, v in rec.meta.items():
            col = _sql_col(str(k))
            if col not in row or row[col] in ("", None):
                row[col] = _sql_cell(v)
            seen_cols.setdefault(col, None)
        if extra_fields is not None:
            for k, v in extra_fields(rec).items():
                col = _sql_col(str(k))
                row[col] = _sql_cell(v)
                seen_cols.setdefault(col, None)
        rows_data.append(row)

    columns = list(seen_cols.keys())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    col_defs = ", ".join('"%s"' % c for c in columns)
    conn.execute("CREATE TABLE records (%s)" % col_defs)
    if rows_data:
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            "INSERT INTO records (%s) VALUES (%s)" % (col_defs, placeholders),
            [[row.get(c) for c in columns] for row in rows_data],
        )
    if table_alias and _sql_col(table_alias) != "records":
        alias = _sql_col(table_alias)
        conn.execute('CREATE VIEW "%s" AS SELECT * FROM records' % alias)
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn, columns


def query_records(
    records: "list[Record]",
    *,
    sql: Optional[str] = None,
    select: Optional[str] = None,
    where: Optional[str] = None,
    order_by: Optional[str] = None,
    group_by: Optional[str] = None,
    limit: Optional[int] = None,
    table_alias: Optional[str] = None,
    extra_fields: "Optional[callable]" = None,
) -> "tuple[list[str], list[dict[str, Any]]]":
    """Execute a read-only SQL query (raw ``sql`` or structured clauses) over ``records``."""
    conn, _ = _build_query_db(records, table_alias=table_alias, extra_fields=extra_fields)
    try:
        if sql and sql.strip():
            stmt = sql.strip().rstrip(";")
        else:
            cols = select.strip() if select and select.strip() else "name, status, folder, title, description"
            parts = ["SELECT", cols, "FROM", table_alias or "records"]
            if where and where.strip():
                parts.extend(["WHERE", where.strip()])
            if group_by and group_by.strip():
                parts.extend(["GROUP BY", group_by.strip()])
            if order_by and order_by.strip():
                parts.extend(["ORDER BY", order_by.strip()])
            if limit is not None and limit > 0:
                parts.extend(["LIMIT", str(int(limit))])
            stmt = " ".join(parts)
        try:
            cur = conn.execute(stmt)
        except sqlite3.Error as exc:
            raise fail("query failed: %s (SQL: %s)" % (exc, stmt))
        cols_out = [d[0] for d in (cur.description or [])]
        rows_out = [dict(zip(cols_out, row)) for row in cur.fetchall()]
        return cols_out, rows_out
    finally:
        conn.close()


def format_query_result(
    columns: "list[str]",
    rows: "list[dict[str, Any]]",
    fmt: str = "table",
) -> str:
    """Format query output as ``table``, ``json``, ``tsv``, or ``md``."""
    fmt = fmt.lower()
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    if not rows:
        return "(0 rows)\n" if fmt in ("table", "md") else ""
    str_rows = [["" if row.get(c) is None else str(row.get(c)) for c in columns] for row in rows]
    if fmt == "tsv":
        lines = ["\t".join(columns)] + ["\t".join(r.replace("\t", " ").replace("\n", " ") for r in row) for row in str_rows]
        return "\n".join(lines) + "\n"
    widths = [max([len(c)] + [len(r[i].replace("\n", " ")) for r in str_rows]) for i, c in enumerate(columns)]
    if fmt == "md":
        head = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        body = ["| " + " | ".join(cell.replace("\n", " ").replace("|", "\\|") for cell in row) + " |" for row in str_rows]
        return "\n".join([head, sep] + body) + "\n"
    # table
    head = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "  ".join("-" * widths[i] for i in range(len(columns)))
    body = ["  ".join(row[i].replace("\n", " ").ljust(widths[i]) for i in range(len(columns))) for row in str_rows]
    return "\n".join([head, sep] + body) + "\n"


# --------------------------------------------------------------------------- #
# Filesystem folder store
# --------------------------------------------------------------------------- #


def apply_body_edit(
    rec: Record,
    *,
    body: Optional[str] = None,
    section: Optional[str] = None,
    append: Optional[str] = None,
    replace: "Optional[tuple[str, str]]" = None,
) -> None:
    """Change ``rec.body`` in memory: replace it, or append to / replace text in it or one of its sections."""
    if body is not None:
        rec.body = body
    elif section and append is not None:
        rec.append_to_section(section, append)
    elif section and replace is not None:
        sec_text = rec.section_text(section)
        old, new = replace
        if old not in sec_text:
            raise fail("%s: %r not found in section %r" % (rec.name, old, section))
        rec.set_section(section, sec_text.split("\n", 1)[1].replace(old, new))
    elif append is not None:
        rec.body = rec.body.rstrip("\r\n") + "\n\n" + append.strip("\r\n") + "\n"
    elif replace is not None:
        rec.replace_in_body(replace[0], replace[1])
    else:
        raise fail("pass --body, --append, or --replace")


class FolderStore:
    """Markdown+frontmatter store for any folder (flat or status-folder layout)."""

    def __init__(
        self,
        root: Path,
        *,
        status_folders: "Optional[dict[str, str]]" = None,
        default_status: str = "active",
        key_order: "tuple[str, ...]" = (),
        table_alias: Optional[str] = None,
    ):
        self.root = Path(root)
        self.status_folders = dict(status_folders) if status_folders else None
        self.default_status = default_status
        self.key_order = key_order
        self.table_alias = table_alias

    def status_dir(self, status: str) -> Path:
        if not self.status_folders:
            return self.root
        folder = self.status_folders.get(status, status)
        return self.root / folder if folder else self.root

    def _scan_dir(self, base: Path, out: "list[Record]", *, recursive: bool = False) -> None:
        if not base.is_dir():
            return
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.is_file():
                if entry.suffix.lower() != ".md" or entry.name.lower() in _IGNORED_NAMES:
                    continue
                if entry.name.lower() == "readme.md":
                    continue
                try:
                    out.append(
                        Record.load(
                            entry,
                            root=self.root,
                            status_folders=self.status_folders,
                            key_order=self.key_order,
                        )
                    )
                except UnicodeDecodeError:
                    _LOGGER.warning("skipping %s: not UTF-8", entry)
            elif entry.is_dir() and entry.name not in _NON_RECORD_DIRS:
                readme = entry / "README.md"
                if readme.is_file():
                    try:
                        out.append(
                            Record.load(
                                readme,
                                root=self.root,
                                status_folders=self.status_folders,
                                key_order=self.key_order,
                            )
                        )
                    except UnicodeDecodeError:
                        _LOGGER.warning("skipping %s: not UTF-8", readme)
                elif recursive:
                    self._scan_dir(entry, out, recursive=True)

    def all(self) -> "list[Record]":
        if not self.root.is_dir():
            return []
        found: "list[Record]" = []
        if self.status_folders:
            seen_dirs: "set[Path]" = set()
            for folder in self.status_folders.values():
                d = (self.root / folder) if folder else self.root
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                self._scan_dir(d, found, recursive=False)
            # Also pick up any extra status folders or legacy top-level files
            if self.root not in seen_dirs:
                self._scan_dir(self.root, found, recursive=False)
            for child in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and child not in seen_dirs and child.name not in _NON_RECORD_DIRS and not child.name.startswith("."):
                    if not (child / "README.md").is_file():
                        self._scan_dir(child, found, recursive=False)
        else:
            self._scan_dir(self.root, found, recursive=True)
        return found

    def get(self, name: str) -> Optional[Record]:
        wanted = name[:-3] if name.endswith(".md") else name
        wanted = wanted.replace("\\", "/").strip("/")
        slug_dash = slugify_name(wanted, sep="-", max_len=0)
        slug_under = slugify_name(wanted, sep="_", max_len=0)
        for rec in self.all():
            candidates = {rec.name, rec.path.stem}
            if rec.is_complex:
                candidates.add(rec.path.parent.name)
            if wanted in candidates or slug_dash in candidates or slug_under in candidates:
                return rec
        return None

    def require(self, name: str) -> Record:
        rec = self.get(name)
        if rec is None:
            raise fail("no record %r under %s" % (name, self.root))
        return rec

    def add(
        self,
        name: str,
        *,
        meta: "Optional[dict[str, Any]]" = None,
        body: str = "",
        status: Optional[str] = None,
        complex_dir: bool = False,
    ) -> Record:
        slug = slugify_name(name, sep="-", max_len=60)
        if not slug or slug in ("readme", "index"):
            raise fail("invalid record name %r" % (name,))
        if self.get(slug):
            raise fail("record %r already exists under %s" % (slug, self.root))
        st = status or self.default_status
        rec_meta: "dict[str, Any]" = {"created": today_iso()}
        if meta:
            rec_meta.update(meta)
        if not self.status_folders and st:
            rec_meta["status"] = st
        dest_dir = self.status_dir(st)
        path = (dest_dir / slug / "README.md") if complex_dir else (dest_dir / (slug + ".md"))
        rec = Record(path, rec_meta, body, root=self.root, key_order=self.key_order, status_folders=self.status_folders)
        rec.save()
        return rec

    def set_attrs(
        self,
        name: str,
        updates: "dict[str, Any]",
        *,
        unset: "Optional[list[str]]" = None,
    ) -> Record:
        rec = self.require(name)
        for k in (unset or []):
            rec.meta.pop(k, None)
        for k, v in updates.items():
            if v is None:
                rec.meta.pop(k, None)
            else:
                rec.meta[k] = v
        rec.meta["updated"] = today_iso()
        rec.save()
        return rec

    def edit_body(
        self,
        name: str,
        *,
        body: Optional[str] = None,
        section: Optional[str] = None,
        append: Optional[str] = None,
        replace: "Optional[tuple[str, str]]" = None,
    ) -> Record:
        rec = self.require(name)
        apply_body_edit(rec, body=body, section=section, append=append, replace=replace)
        rec.meta["updated"] = today_iso()
        rec.save()
        return rec

    def move(self, name: str, new_status: str) -> Record:
        rec = self.require(name)
        new_status = new_status.strip().lower()
        if not self.status_folders:
            rec.meta["status"] = new_status
        rec.meta["updated"] = today_iso()
        rec.save()
        dest_dir = self.status_dir(new_status)
        if not self.status_folders and new_status:
            dest_dir = self.root / new_status
        if rec.is_complex:
            src_dir = rec.path.parent
            dest_subdir = dest_dir / src_dir.name
            if dest_subdir != src_dir:
                legacy_dir = self.root / src_dir.name
                if dest_subdir.exists() or (new_status == self.default_status and legacy_dir != src_dir and legacy_dir.exists()):
                    existing = dest_subdir if dest_subdir.exists() else legacy_dir
                    raise fail("cannot move %r: %s already exists" % (rec.name, existing))
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_dir), str(dest_subdir))
                rec.path = dest_subdir / rec.path.name
        else:
            dest_file = dest_dir / rec.path.name
            if dest_file != rec.path:
                legacy_file = self.root / rec.path.name
                if dest_file.exists() or (new_status == self.default_status and legacy_file != rec.path and legacy_file.exists()):
                    existing = dest_file if dest_file.exists() else legacy_file
                    raise fail("cannot move %r: %s already exists" % (rec.name, existing))
                dest_dir.mkdir(parents=True, exist_ok=True)
                rec.path.replace(dest_file)
                rec.path = dest_file
        return rec

    def remove(self, name: str, *, force: bool = False) -> Path:
        rec = self.require(name)
        if rec.status in ("done", "processed") and not force:
            raise fail("%s has status %r; pass --force to remove" % (rec.name, rec.status))
        if rec.is_complex:
            target = rec.path.parent
            if rec.extras and not force:
                raise fail("%s has extra files (%d); pass --force to remove directory" % (rec.name, len(rec.extras)))
            shutil.rmtree(target)
            return target
        path = rec.path
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
        return query_records(
            self.all(),
            sql=sql,
            select=select,
            where=where,
            order_by=order_by,
            group_by=group_by,
            limit=limit,
            table_alias=self.table_alias,
        )


# --------------------------------------------------------------------------- #
# Pluggable record providers (Markdown default; GitHub / GitLab / Jira / AI6)
# --------------------------------------------------------------------------- #


class RecordProvider(ABC):
    """Backend interface for storing and querying plans/findings/records."""

    kind: str = ""

    @abstractmethod
    def capabilities(self) -> "dict[str, Any]":
        """Report provider capabilities and configuration."""

    @abstractmethod
    def list_records(self, *, status: Optional[str] = None) -> "list[dict[str, Any]]":
        """List records."""

    @abstractmethod
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
        """Query records with SQL or filter clauses."""

    @abstractmethod
    def get(self, name: str) -> "dict[str, Any]":
        """Fetch one record by name/id."""

    @abstractmethod
    def create(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: str = "") -> "dict[str, Any]":
        """Create a record."""

    @abstractmethod
    def update(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: Optional[str] = None) -> "dict[str, Any]":
        """Update record metadata or body."""

    @abstractmethod
    def move(self, name: str, new_status: str) -> "dict[str, Any]":
        """Transition a record to ``new_status``."""

    @abstractmethod
    def delete(self, name: str, *, force: bool = False) -> str:
        """Delete a record."""


class MarkdownProvider(RecordProvider):
    """Default local Markdown+frontmatter file provider."""

    kind = "markdown"

    def __init__(self, store: FolderStore):
        self.store = store

    def capabilities(self) -> "dict[str, Any]":
        return {
            "kind": self.kind,
            "root": str(self.store.root),
            "sql": True,
            "complex_dirs": True,
            "status_folders": self.store.status_folders or {},
        }

    def list_records(self, *, status: Optional[str] = None) -> "list[dict[str, Any]]":
        recs = self.store.all()
        if status:
            recs = [r for r in recs if r.status == status]
        return [r.as_dict(include_body=False) for r in recs]

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
        return self.store.query(
            sql=sql,
            select=select,
            where=where,
            order_by=order_by,
            group_by=group_by,
            limit=limit,
        )

    def get(self, name: str) -> "dict[str, Any]":
        return self.store.require(name).as_dict(include_body=True)

    def create(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: str = "") -> "dict[str, Any]":
        return self.store.add(name, meta=meta, body=body).as_dict(include_body=True)

    def update(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: Optional[str] = None) -> "dict[str, Any]":
        if meta:
            rec = self.store.set_attrs(name, meta)
        else:
            rec = self.store.require(name)
        if body is not None:
            rec = self.store.edit_body(name, body=body)
        return rec.as_dict(include_body=True)

    def move(self, name: str, new_status: str) -> "dict[str, Any]":
        return self.store.move(name, new_status).as_dict(include_body=False)

    def delete(self, name: str, *, force: bool = False) -> str:
        return str(self.store.remove(name, force=force))


# --------------------------------------------------------------------------- #
# External trackers: the issue's key is the record's name, its status a label or a workflow state.
# Neither is repeated in the issue's text; the other frontmatter fields ride in a block at the top of its body.
# --------------------------------------------------------------------------- #

# Fields the tracker itself holds (title, state, dates, address): never written into the body block.
_TRACKER_FIELDS = frozenset(("name", "status", "title", "url", "created", "updated", "number", "path", "folder"))


def _words(value: Any) -> "list[str]":
    if isinstance(value, str):
        return [w.strip().lower() for w in value.split(",") if w.strip()]
    return [str(w).strip().lower() for w in (value or []) if str(w).strip()]


class ExternalTrackerProvider(RecordProvider):
    """Adapter for a remote issue tracker (GitHub, GitLab, Jira, AI6).

    A subclass that talks to its tracker sets ``wired`` and implements the ``_items``/``_item``/``_make``/``_edit``/
    ``_transition``/``_remove`` hooks, which exchange issues as dicts with ``key``, ``title``, ``body``, ``status``,
    ``created``, ``updated`` and ``url``. Credentials are the person's own CLI login or a named environment variable.
    """

    required_keys: "tuple[str, ...]" = ()
    wired = False
    # What opens and closes the block of frontmatter fields at the top of an issue's body.
    block_open = "<!-- records"
    block_close = "-->"

    def __init__(self, kind: str, config: "Optional[dict[str, Any]]" = None):
        self.kind = kind
        self.config = dict(config or {})

    def _require_configured(self) -> None:
        missing = [k for k in self.required_keys if not self.config.get(k)]
        if missing:
            raise fail(
                "provider %r is missing configuration keys: %s"
                % (self.kind, ", ".join(missing))
            )

    def capabilities(self) -> "dict[str, Any]":
        return {
            "kind": self.kind,
            "configured": all(bool(self.config.get(k)) for k in self.required_keys),
            "wired": self.wired,
            "required_keys": list(self.required_keys),
            "config": self.config,
        }

    def _not_wired(self, op: str) -> "Any":
        self._require_configured()
        raise fail(
            "external provider %r (%s) requires its transport/CLI adapter to execute %s"
            % (self.kind, self.config.get("repo") or self.config.get("project") or self.config.get("url") or "", op)
        )

    def _ready(self, op: str) -> None:
        if not self.wired:
            self._not_wired(op)
        self._require_configured()

    # ---- configuration ---------------------------------------------------

    @property
    def default_status(self) -> str:
        return str(self.config.get("default_status") or "active").lower()

    @property
    def closed_statuses(self) -> "list[str]":
        return _words(self.config.get("closed_statuses")) or ["done", "processed"]

    @property
    def label(self) -> str:
        return str(self.config.get("repo") or self.config.get("project") or self.kind)

    def _norm_key(self, name: str) -> str:
        return str(name).strip().lstrip("#")

    # ---- the body block ---------------------------------------------------

    def _decode(self, text: str) -> "tuple[dict[str, Any], str]":
        text = (text or "").replace("\r\n", "\n")
        head = self.block_open + "\n"
        if not text.startswith(head):
            return {}, text
        end = text.find("\n" + self.block_close, len(head) - 1)
        if end < 0:
            return {}, text
        meta, _, _ = parse_frontmatter("---\n" + text[len(head):end] + "\n---\n", typed=True)
        return meta, text[end + 1 + len(self.block_close):].lstrip("\n")

    def _encode(self, meta: "dict[str, Any]", body: str) -> str:
        fields = {k: v for k, v in meta.items() if k not in _TRACKER_FIELDS}
        body = (body or "").strip("\r\n")
        if not any(v not in (None, "", []) for v in fields.values()):
            return body + "\n" if body else ""
        block = render_frontmatter(fields, "").split("\n", 1)[1].rsplit("---", 1)[0].rstrip("\n")
        return "%s\n%s\n%s\n\n%s" % (self.block_open, block, self.block_close, body + "\n" if body else "")

    def _record(self, item: "dict[str, Any]") -> Record:
        meta, body = self._decode(str(item.get("body") or ""))
        meta.update(
            title=item.get("title") or "",
            status=item.get("status") or "",
            created=item.get("created") or "",
            updated=item.get("updated") or "",
            url=item.get("url") or "",
        )
        return Record(Path(self.label) / str(item["key"]), meta, body, name=str(item["key"]))

    @staticmethod
    def _as_dict(rec: Record, *, include_body: bool) -> "dict[str, Any]":
        out = rec.as_dict(include_body=include_body)
        out["path"] = rec.meta.get("url") or out["path"]
        out["folder"] = ""
        return out

    # ---- hooks a wired provider implements ---------------------------------

    def _items(self) -> "list[dict[str, Any]]":
        raise NotImplementedError

    def _item(self, key: str) -> "dict[str, Any]":
        raise NotImplementedError

    def _make(self, title: str, body: str, status: str) -> str:
        raise NotImplementedError

    def _edit(self, item: "dict[str, Any]", title: str, body: str) -> None:
        raise NotImplementedError

    def _transition(self, item: "dict[str, Any]", new_status: str) -> None:
        raise NotImplementedError

    def _remove(self, item: "dict[str, Any]") -> None:
        raise NotImplementedError

    # ---- the provider interface --------------------------------------------

    def records(self) -> "list[Record]":
        self._ready("list")
        return [self._record(i) for i in self._items()]

    def record(self, name: str) -> Record:
        self._ready("get")
        return self._record(self._item(self._norm_key(name)))

    def list_records(self, *, status: Optional[str] = None) -> "list[dict[str, Any]]":
        recs = self.records()
        if status:
            recs = [r for r in recs if r.status == status.lower()]
        return [self._as_dict(r, include_body=False) for r in recs]

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
        return query_records(
            self.records(), sql=sql, select=select, where=where, order_by=order_by, group_by=group_by, limit=limit
        )

    def get(self, name: str) -> "dict[str, Any]":
        return self._as_dict(self.record(name), include_body=True)

    def create(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: str = "") -> "dict[str, Any]":
        """Open an issue titled ``meta['title']`` or else ``name``; the tracker names the record (its key)."""
        self._ready("create")
        fields = dict(meta or {})
        title = str(fields.pop("title", "") or name)
        status = str(fields.pop("status", "") or self.default_status).lower()
        key = self._make(title, self._encode(fields, body), status)
        return self.get(key)

    def update(self, name: str, *, meta: "Optional[dict[str, Any]]" = None, body: Optional[str] = None) -> "dict[str, Any]":
        """Merge ``meta`` (``None`` removes a field; ``title`` and ``status`` go to the tracker) and replace the body."""
        self._ready("update")
        item = self._item(self._norm_key(name))
        fields, old_body = self._decode(str(item.get("body") or ""))
        title = str(item.get("title") or "")
        new_status = None
        for k, v in (meta or {}).items():
            if k == "title" and v:
                title = str(v)
            elif k == "status":
                new_status = str(v).lower() if v else None
            elif k in _TRACKER_FIELDS:
                continue
            elif v is None:
                fields.pop(k, None)
            else:
                fields[k] = v
        self._edit(item, title, self._encode(fields, old_body if body is None else body))
        if new_status and new_status != item.get("status"):
            self._transition(item, new_status)
        return self.get(item["key"])

    def move(self, name: str, new_status: str) -> "dict[str, Any]":
        self._ready("move")
        item = self._item(self._norm_key(name))
        new_status = new_status.strip().lower()
        if item.get("status") != new_status:
            self._transition(item, new_status)
        return self._as_dict(self.record(item["key"]), include_body=False)

    def delete(self, name: str, *, force: bool = False) -> str:
        self._ready("delete")
        item = self._item(self._norm_key(name))
        if item.get("status") in self.closed_statuses and not force:
            raise fail("%s has status %r; pass --force to remove" % (item["key"], item.get("status")))
        self._remove(item)
        return "%s/%s" % (self.label, item["key"])


def _cli_command(config: "dict[str, Any]", default: str, hint: str) -> "list[str]":
    """The program to run: ``command`` from the configuration (a string or an argument list), else ``default`` on PATH."""
    given = config.get("command")
    if given:
        return [str(a) for a in given] if isinstance(given, (list, tuple)) else [str(given)]
    found = shutil.which(default)
    if not found:
        raise fail("%r is not on PATH: %s" % (default, hint))
    return [found]


def _run_cli(argv: "list[str]", *, stdin: Optional[str] = None) -> str:
    try:
        done = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False
        )
    except FileNotFoundError:
        raise fail("cannot run %r" % (argv[0],))
    except subprocess.TimeoutExpired:
        raise fail("%s timed out" % " ".join(argv[:3]))
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise fail("%s failed (exit %d): %s" % (" ".join(argv[:3]), done.returncode, detail[-1] if detail else "no output"))
    return done.stdout


class _LabelStatusProvider(ExternalTrackerProvider):
    """Status as one ``<status_prefix><status>`` label; a closed status also closes the issue."""

    @property
    def status_prefix(self) -> str:
        return str(self.config.get("status_prefix") or "status:")

    def _status_of(self, labels: "list[str]", closed: bool) -> str:
        for label in labels:
            if label.lower().startswith(self.status_prefix.lower()):
                return label[len(self.status_prefix):].strip().lower()
        return self.closed_statuses[0] if closed else self.default_status

    def _status_labels(self, item: "dict[str, Any]") -> "list[str]":
        return [l for l in item.get("labels", []) if l.lower().startswith(self.status_prefix.lower())]


class GitHubProvider(_LabelStatusProvider):
    """GitHub issues (or pull requests, ``kind = "pr"``) through the person's logged-in ``gh``."""

    required_keys = ("repo",)
    wired = True
    _FIELDS = "number,title,body,labels,state,createdAt,updatedAt,url"

    def __init__(self, config: "Optional[dict[str, Any]]" = None):
        super().__init__("github", config)

    @property
    def _noun(self) -> str:
        return "pr" if str(self.config.get("kind") or "issue").lower() in ("pr", "pull_request") else "issue"

    def _gh(self, *args: str, stdin: Optional[str] = None) -> str:
        cmd = _cli_command(self.config, "gh", "install GitHub CLI and run `gh auth login`")
        return _run_cli(cmd + list(args) + ["--repo", str(self.config["repo"])], stdin=stdin)

    def _normalize(self, raw: "dict[str, Any]") -> "dict[str, Any]":
        labels = [str(l.get("name", "")) for l in raw.get("labels") or []]
        closed = str(raw.get("state", "")).upper() != "OPEN"
        return {
            "key": str(raw["number"]),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "labels": labels,
            "open": not closed,
            "status": self._status_of(labels, closed),
            "created": raw.get("createdAt") or "",
            "updated": raw.get("updatedAt") or "",
            "url": raw.get("url") or "",
        }

    def _items(self) -> "list[dict[str, Any]]":
        out = self._gh(
            self._noun, "list", "--state", "all", "--limit", str(int(self.config.get("limit") or 1000)),
            "--json", self._FIELDS,
        )
        return [self._normalize(r) for r in json.loads(out or "[]")]

    def _item(self, key: str) -> "dict[str, Any]":
        if not key.isdigit():
            raise fail("%r: a GitHub record's name is its number" % (key,))
        return self._normalize(json.loads(self._gh(self._noun, "view", key, "--json", self._FIELDS)))

    def _ensure_label(self, status: str) -> str:
        label = self.status_prefix + status
        self._gh("label", "create", label, "--force")
        return label

    def _make(self, title: str, body: str, status: str) -> str:
        if self._noun == "pr":
            raise fail("a pull request is opened from a branch, not created as a record")
        label = self._ensure_label(status)
        url = self._gh("issue", "create", "--title", title, "--body-file", "-", "--label", label, stdin=body).strip()
        key = url.rstrip("/").rsplit("/", 1)[-1]
        if not key.isdigit():
            raise fail("gh did not print the new issue's address: %r" % (url,))
        if status in self.closed_statuses:
            self._gh("issue", "close", key)
        return key

    def _edit(self, item: "dict[str, Any]", title: str, body: str) -> None:
        self._gh(self._noun, "edit", item["key"], "--title", title, "--body-file", "-", stdin=body)

    def _transition(self, item: "dict[str, Any]", new_status: str) -> None:
        label = self._ensure_label(new_status)
        args = [self._noun, "edit", item["key"], "--add-label", label]
        for old in self._status_labels(item):
            if old != label:
                args += ["--remove-label", old]
        self._gh(*args)
        closing = new_status in self.closed_statuses
        if closing and item["open"]:
            self._gh(self._noun, "close", item["key"])
        elif not closing and not item["open"]:
            self._gh(self._noun, "reopen", item["key"])

    def _remove(self, item: "dict[str, Any]") -> None:
        if self._noun == "pr":
            raise fail("a pull request cannot be deleted; move it to a closed status")
        self._gh("issue", "delete", item["key"], "--yes")


class GitLabProvider(_LabelStatusProvider):
    """GitLab issues (or merge requests, ``kind = "mr"``) through the person's logged-in ``glab api``."""

    required_keys = ("project",)
    wired = True

    def __init__(self, config: "Optional[dict[str, Any]]" = None):
        super().__init__("gitlab", config)

    @property
    def _is_mr(self) -> bool:
        return str(self.config.get("kind") or "issue").lower() in ("mr", "merge_request")

    @property
    def _endpoint(self) -> str:
        project = urllib.parse.quote(str(self.config["project"]), safe="")
        return "projects/%s/%s" % (project, "merge_requests" if self._is_mr else "issues")

    def _api(self, path: str, method: str = "GET", payload: "Optional[dict[str, Any]]" = None) -> Any:
        cmd = _cli_command(self.config, "glab", "install GitLab CLI and run `glab auth login`")
        argv = cmd + ["api", path, "-X", method]
        stdin = None
        if payload is not None:
            argv += ["-H", "Content-Type: application/json", "--input", "-"]
            stdin = json.dumps(payload)
        out = _run_cli(argv, stdin=stdin).strip()
        return json.loads(out) if out else None

    def _normalize(self, raw: "dict[str, Any]") -> "dict[str, Any]":
        labels = [str(l) for l in raw.get("labels") or []]
        closed = str(raw.get("state", "")) != "opened"
        return {
            "key": str(raw["iid"]),
            "title": raw.get("title") or "",
            "body": raw.get("description") or "",
            "labels": labels,
            "open": not closed,
            "status": self._status_of(labels, closed),
            "created": raw.get("created_at") or "",
            "updated": raw.get("updated_at") or "",
            "url": raw.get("web_url") or "",
        }

    def _items(self) -> "list[dict[str, Any]]":
        out: "list[dict[str, Any]]" = []
        page = 1
        while True:
            rows = self._api("%s?state=all&per_page=100&page=%d" % (self._endpoint, page)) or []
            out.extend(self._normalize(r) for r in rows)
            if len(rows) < 100:
                return out
            page += 1

    def _item(self, key: str) -> "dict[str, Any]":
        if not key.isdigit():
            raise fail("%r: a GitLab record's name is its number" % (key,))
        return self._normalize(self._api("%s/%s" % (self._endpoint, key)))

    def _make(self, title: str, body: str, status: str) -> str:
        if self._is_mr:
            raise fail("a merge request is opened from a branch, not created as a record")
        raw = self._api(self._endpoint, "POST", {
            "title": title, "description": body, "labels": self.status_prefix + status,
        })
        key = str(raw["iid"])
        if status in self.closed_statuses:
            self._api("%s/%s" % (self._endpoint, key), "PUT", {"state_event": "close"})
        return key

    def _edit(self, item: "dict[str, Any]", title: str, body: str) -> None:
        self._api("%s/%s" % (self._endpoint, item["key"]), "PUT", {"title": title, "description": body})

    def _transition(self, item: "dict[str, Any]", new_status: str) -> None:
        label = self.status_prefix + new_status
        payload: "dict[str, Any]" = {"add_labels": label}
        old = [l for l in self._status_labels(item) if l != label]
        if old:
            payload["remove_labels"] = ",".join(old)
        closing = new_status in self.closed_statuses
        if closing and item["open"]:
            payload["state_event"] = "close"
        elif not closing and not item["open"]:
            payload["state_event"] = "reopen"
        self._api("%s/%s" % (self._endpoint, item["key"]), "PUT", payload)

    def _remove(self, item: "dict[str, Any]") -> None:
        self._api("%s/%s" % (self._endpoint, item["key"]), "DELETE")


class JiraProvider(ExternalTrackerProvider):
    """Jira issues over REST API v2. Credentials come from environment variables the configuration names
    (``token_env``, default ``JIRA_API_TOKEN``; ``user`` or ``user_env`` for basic auth, else the token is a bearer
    token); nothing is written to a file. ``status_map`` maps record statuses to Jira status names."""

    required_keys = ("url", "project")
    wired = True
    block_open = "{code:records}"
    block_close = "{code}"
    _FIELDS = "summary,description,status,created,updated"

    def __init__(self, config: "Optional[dict[str, Any]]" = None):
        super().__init__("jira", config)

    def _norm_key(self, name: str) -> str:
        return str(name).strip().upper()

    @property
    def _status_map(self) -> "dict[str, str]":
        raw = self.config.get("status_map")
        return {str(k).lower(): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _status_of(self, jira_name: str) -> str:
        for status, name in self._status_map.items():
            if name.lower() == jira_name.lower():
                return status
        return jira_name.lower()

    def _auth(self) -> str:
        env = str(self.config.get("token_env") or "JIRA_API_TOKEN")
        token = os.environ.get(env, "")
        if not token:
            raise fail("set the environment variable %s to a Jira API token" % env)
        user = self.config.get("user") or os.environ.get(str(self.config.get("user_env") or "-"), "")
        if user:
            return "Basic " + base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
        return "Bearer " + token

    def _call(self, path: str, method: str = "GET", payload: "Optional[dict[str, Any]]" = None) -> Any:
        base = str(self.config["url"]).rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise fail("jira url must start with https:// (got %r)" % base)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={
            "Authorization": self._auth(), "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip().replace("\n", " ")[:200]
            raise fail("jira %s %s: HTTP %d %s" % (method, path.split("?")[0], exc.code, detail))
        except urllib.error.URLError as exc:
            raise fail("jira %s: %s" % (base, exc.reason))
        return json.loads(text) if text.strip() else None

    def _normalize(self, raw: "dict[str, Any]") -> "dict[str, Any]":
        f = raw.get("fields") or {}
        return {
            "key": raw["key"],
            "title": f.get("summary") or "",
            "body": f.get("description") or "",
            "status": self._status_of(str((f.get("status") or {}).get("name") or "")),
            "created": f.get("created") or "",
            "updated": f.get("updated") or "",
            "url": "%s/browse/%s" % (str(self.config["url"]).rstrip("/"), raw["key"]),
        }

    def _items(self) -> "list[dict[str, Any]]":
        jql = urllib.parse.quote('project = "%s" ORDER BY created ASC' % self.config["project"])
        cloud = str(self.config.get("flavor") or "cloud").lower() != "server"
        out: "list[dict[str, Any]]" = []
        token, start = "", 0
        while True:
            if cloud:
                path = "/rest/api/2/search/jql?jql=%s&fields=%s&maxResults=100" % (jql, self._FIELDS)
                if token:
                    path += "&nextPageToken=" + urllib.parse.quote(token)
            else:
                path = "/rest/api/2/search?jql=%s&fields=%s&maxResults=100&startAt=%d" % (jql, self._FIELDS, start)
            page = self._call(path) or {}
            rows = page.get("issues") or []
            out.extend(self._normalize(r) for r in rows)
            if cloud:
                token = page.get("nextPageToken") or ""
                if not token or not rows:
                    return out
            else:
                start += len(rows)
                if not rows or start >= int(page.get("total") or 0):
                    return out

    def _item(self, key: str) -> "dict[str, Any]":
        return self._normalize(self._call("/rest/api/2/issue/%s?fields=%s" % (urllib.parse.quote(key), self._FIELDS)))

    def _make(self, title: str, body: str, status: str) -> str:
        raw = self._call("/rest/api/2/issue", "POST", {"fields": {
            "project": {"key": str(self.config["project"])},
            "summary": title,
            "description": body,
            "issuetype": {"name": str(self.config.get("issue_type") or "Task")},
        }})
        item = self._item(raw["key"])
        if item["status"] != status and status in self._status_map:
            self._transition(item, status)
        return raw["key"]

    def _edit(self, item: "dict[str, Any]", title: str, body: str) -> None:
        self._call("/rest/api/2/issue/" + urllib.parse.quote(item["key"]), "PUT",
                   {"fields": {"summary": title, "description": body}})

    def _transition(self, item: "dict[str, Any]", new_status: str) -> None:
        target = self._status_map.get(new_status, new_status)
        base = "/rest/api/2/issue/%s/transitions" % urllib.parse.quote(item["key"])
        options = (self._call(base) or {}).get("transitions") or []
        for t in options:
            if str((t.get("to") or {}).get("name", "")).lower() == target.lower():
                self._call(base, "POST", {"transition": {"id": t["id"]}})
                return
        names = ", ".join(sorted(str((t.get("to") or {}).get("name", "")) for t in options)) or "none"
        raise fail("%s cannot move to %r from here (Jira offers: %s)" % (item["key"], target, names))

    def _remove(self, item: "dict[str, Any]") -> None:
        self._call("/rest/api/2/issue/" + urllib.parse.quote(item["key"]), "DELETE")


class Ai6Provider(ExternalTrackerProvider):
    required_keys = ("operation",)

    def __init__(self, config: "Optional[dict[str, Any]]" = None):
        super().__init__("ai6", config)


class ProviderStore:
    """The part of :class:`FolderStore` the commands use, over an external provider's records."""

    def __init__(self, provider: "ExternalTrackerProvider"):
        self.provider = provider
        self.root = Path(provider.label)

    def all(self) -> "list[Record]":
        return self.provider.records()

    def require(self, name: str) -> Record:
        return self.provider.record(name)

    def query(self, **kwargs: Any) -> "tuple[list[str], list[dict[str, Any]]]":
        return self.provider.query(**kwargs)

    def add(
        self,
        name: str,
        *,
        meta: "Optional[dict[str, Any]]" = None,
        body: str = "",
        status: Optional[str] = None,
        complex_dir: bool = False,
    ) -> Record:
        if complex_dir:
            raise fail("a tracker's issue is one record: --complex is for folders")
        fields = dict(meta or {})
        if status:
            fields["status"] = status
        return self.provider.record(self.provider.create(name, meta=fields, body=body)["name"])

    def set_attrs(self, name: str, updates: "dict[str, Any]", *, unset: "Optional[list[str]]" = None) -> Record:
        fields = {k: None for k in (unset or [])}
        fields.update(updates)
        return self.provider.record(self.provider.update(name, meta=fields)["name"])

    def edit_body(
        self,
        name: str,
        *,
        body: Optional[str] = None,
        section: Optional[str] = None,
        append: Optional[str] = None,
        replace: "Optional[tuple[str, str]]" = None,
    ) -> Record:
        rec = self.require(name)
        apply_body_edit(rec, body=body, section=section, append=append, replace=replace)
        return self.provider.record(self.provider.update(name, body=rec.body)["name"])

    def move(self, name: str, new_status: str) -> Record:
        return self.provider.record(self.provider.move(name, new_status)["name"])

    def remove(self, name: str, *, force: bool = False) -> Path:
        return Path(self.provider.delete(name, force=force))


BUILTIN_PROVIDERS: "dict[str, type[ExternalTrackerProvider]]" = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "jira": JiraProvider,
    "ai6": Ai6Provider,
}


def load_provider_settings(agents_root: Path, scope_key: str) -> "dict[str, Any]":
    """Read provider configuration for ``scope_key`` (``plans``, ``findings``, ``records``)
    from ``<agents_root>/records.toml`` or ``<agents_root>/agent-tools.toml``."""
    if tomllib is None:
        return {"provider": "markdown"}
    for candidate in (agents_root / "records.toml", agents_root / "agent-tools.toml"):
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        records_cfg = data.get("records")
        if isinstance(records_cfg, dict) and isinstance(records_cfg.get(scope_key), dict):
            cfg = dict(records_cfg[scope_key])
            cfg.setdefault("provider", "markdown")
            return cfg
        direct = data.get(scope_key)
        if isinstance(direct, dict):
            cfg = dict(direct)
            cfg.setdefault("provider", "markdown")
            return cfg
    return {"provider": "markdown"}


def open_provider(store: FolderStore, agents_root: Optional[Path] = None, scope_key: str = "records") -> RecordProvider:
    """Resolve the active :class:`RecordProvider` for ``store`` (default: ``markdown``)."""
    cfg = load_provider_settings(agents_root or store.root.parent, scope_key)
    kind = str(cfg.get("provider", "markdown")).lower()
    if kind == "markdown":
        return MarkdownProvider(store)
    cls = BUILTIN_PROVIDERS.get(kind)
    if cls is None:
        known = ", ".join(["markdown"] + sorted(BUILTIN_PROVIDERS))
        raise fail("unknown record provider %r (known: %s)" % (kind, known))
    return cls(cfg)
