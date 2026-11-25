---
name: blob-scan
description: Find blobs a malware or archive scanner may refuse (archives, encrypted data, executables, key files, git packs, opaque binaries) in every ref of a git repository.
---

# blob-scan

Use `blob-scan` (`skills/blob-scan/blob_scan.py`) before a push to a public remote, on any repository, to see what a scanner would flag
anywhere in its history: archives and compressed data, encrypted or password-protected archives, executables, key and certificate files,
private keys in text, git pack files and opaque high-entropy data. Known media (PNG, JPEG, GIF, WebP, ICO, fonts, SVG) and text pass.

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/blob-scan/blob_scan.py <repo> [--refs <pattern> ...]
```

It prints each flagged kind with its blobs, then `PASS|FAIL: N flagged, M paths scanned`. Exit 0 clean, 1 flagged, 2 usage.

## Relation to `build.py scan`

The release overlay's shared `tools/build.py scan` keeps this report format but is not the same check: it runs inside a repository that
adopted `tools/build.py` and its `build.cfg`, scans a commit's tree (`--tree`, `--all-refs`, or the named refs), reads `[deps] allow`,
also flags symbolic links, paths below `.git` and every unrecognised binary, and `repo-guard push` calls it. `blob-scan` needs
nothing but git and one repository path, and treats an unrecognised binary as a note, not a flag. Use `build.py scan` where the
repository has it; use `blob-scan` everywhere else.
