#!/usr/bin/env python3
"""Validate every overlay and the catalog with coqui-aos (the Agent Overlay Specification reader).

    pip install "coqui-aos @ git+https://github.com/Boriken-Dev/coqui-aos#subdirectory=python"
    python tools/validate.py

Checks, for each folder under overlays/: its manifest reads and validates against its own files,
its icon is a PNG or WebP within the default limits, its `about` is filled. Then the catalog reads
with no refused entry, lists exactly the folders under overlays/, every relative source names its
folder, every entry's icon exists, and the catalog's summary, homepage and tags agree with the
overlay's own. Exits 1 on any problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coqui_aos import (
    Error,
    PathFS,
    read_catalog,
    read_document,
    read_icon,
    read_overlay,
    read_tree,
    validate,
)

ROOT = Path(__file__).resolve().parent.parent
TAGS = {
    "language", "workflow", "sync", "recovery", "release", "network", "process",
    "javascript", "python", "rust",
}  # fmt: skip


def main() -> int:
    problems: list[str] = []
    fs = PathFS()
    manifests = {}

    folders = sorted(p for p in (ROOT / "overlays").iterdir() if p.is_dir())
    for folder in folders:
        name = folder.name
        try:
            tree, digest = read_tree(fs, folder)
            read = read_overlay(tree)
        except Error as error:
            problems.append(f"{name}: unreadable ({error.class_.value}: {error.subject})")
            continue
        manifest = read.manifest
        manifests[name] = manifest
        for issue in validate(manifest, tree):
            problems.append(f"{name}: {issue.code.value} at {issue.at}")
        if manifest.get("name") != name:
            problems.append(f"{name}: manifest name {manifest.get('name')!r} is not the folder's")
        about = manifest.get("about")
        if not isinstance(about, dict):
            problems.append(f"{name}: no [about] table")
            continue
        for key in ("summary", "icon", "homepage", "tags", "license"):
            if key not in about:
                problems.append(f"{name}: about.{key} missing")
        for tag in about.get("tags", []):
            if tag not in TAGS:
                problems.append(f"{name}: tag {tag!r} is not in the shared list in tools/validate.py")
        icon = about.get("icon")
        if icon:
            try:
                read_icon((folder / icon).read_bytes(), icon)
            except (Error, OSError) as error:
                problems.append(f"{name}: icon {icon}: {getattr(error, 'class_', error)}")

    catalog_path = ROOT / "catalog.toml"
    try:
        catalog = read_catalog(catalog_path.read_bytes(), catalog_path.name, base=str(ROOT))
    except Error as error:
        problems.append(f"catalog.toml: {error.class_.value}")
        catalog = None
    if catalog is not None:
        for key in ("name", "description", "homepage"):
            if not getattr(catalog, key):
                problems.append(f"catalog.toml: {key} missing")
        for key in catalog.ignored:
            problems.append(f"catalog.toml: field left out: {key}")
        for name, why in catalog.overlays.refused:
            problems.append(f"catalog.toml: entry {name} refused ({why.value})")
        listed = set(catalog.overlays.entries)
        if listed != set(manifests):
            problems.append(f"catalog.toml lists {sorted(listed)}; overlays/ holds {sorted(manifests)}")
        document = read_document(catalog_path.read_bytes())
        if not isinstance(document.get("about"), str):
            problems.append("catalog.toml: about missing")
        for name, entry in catalog.overlays.entries.items():
            if entry.source.location.replace("\\", "/").rstrip("/").split("/")[-2:] != ["overlays", name]:
                problems.append(f"catalog.toml: {name} source is {entry.source.location}")
            about = entry.about or {}
            own = manifests.get(name, {}).get("about", {})
            for key in ("summary", "homepage", "tags"):
                if about.get(key) != own.get(key):
                    problems.append(f"{name}: catalog {key} differs from the overlay's own")
            icon = about.get("icon")
            if not icon:
                problems.append(f"{name}: catalog entry has no icon")
            else:
                try:
                    read_icon((ROOT / icon).read_bytes(), icon)
                except (Error, OSError) as error:
                    problems.append(f"{name}: catalog icon {icon}: {getattr(error, 'class_', error)}")

    for line in problems:
        print(line)
    print(f"{len(folders)} overlays, {'FAIL' if problems else 'ok'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
