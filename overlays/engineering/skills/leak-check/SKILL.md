---
name: leak-check
description: Scan a repository working tree and commit messages for private `.agents/` paths, plan names, decision IDs, personal/infra markers, and AI attribution trailers before commit or release.
---

# leak-check

Use `leak-check` (`skills/leak-check/leak_check.py`) before any commit, handoff, push, or release tag to verify no private notes or attribution trailers leak.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/leak-check/leak_check.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `leak-check` directly.

## History scan

`leak-check-history` (`skills/leak-check/leak_check_history.py`) scans the whole history of every repository under a directory, not the
working tree: every blob reachable from any ref, every commit message, every path any commit touched, and every author and committer.
It reads the same pattern file as `leak-check` and labels a hit in a commit someone else authored `UPSTREAM`, so a fork's own history
is not reported as the owner's. The owner's identities are the pattern file's optional `"ours"` list, else `git config user.name` and
`user.email`.

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/leak-check/leak_check_history.py ROOT [REPO_NAME] [BLOB_CAP] [--patterns FILE]
```

Exit 0: no repository has hits of its own. 1: at least one has. 3: no pattern file.
