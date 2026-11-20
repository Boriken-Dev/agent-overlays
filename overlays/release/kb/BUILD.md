# Shared repository tools

`repo/tools/` holds files a repository copies verbatim into its own `tools/`:

| File | What it does |
| --- | --- |
| `build.py` | The deps branch (vendored crates, pub packages, CDN files), the archive and binary scan, the SDK lock, offline and release builds, Flutter helpers |
| `public_api.py` | Checks or rewrites the public API snapshots of Rust crates |
| `apk.py` | Checks a built Android APK: forbidden strings, permissions, ABIs, required libraries |

Each is standard library only, Python 3.11.0 or later, and is configured by one file, `tools/build.cfg` (TOML). A section
the file leaves out is off. Flags are documented by `build.py --help` and `build.py help`; every `build.cfg` key with its
default is printed by `build.py help config`, and a key that is not in that table is refused with its path.

## Adopting

1. Make the first copy from a checkout of this repository, naming the repository to copy into:
   `python overlays/release/repo/tools/build.py --root REPO self update COMMIT --from CHECKOUT`. `COMMIT` is the commit of
   this repository to copy from (a branch or tag works too); without `--from` the files are downloaded from GitHub.
2. Write `tools/build.cfg`. `[tools] files = ["build.py"]` is the default; list every shared file the repository copies
   (`"public_api.py"`, `"apk.py"`).
3. Run `python tools/build.py self check`. CI runs the same command.

## Keeping the copy equal

Line 3 of every shared file is `# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/FILE, at commit COMMIT.`; in
this repository `COMMIT` is the word `SOURCE`, since a commit cannot hold its own hash. `self check` downloads each listed
file at the commit its line 3 names, puts `SOURCE` back and compares bytes. It fails on a difference, on a listed file that
is missing, on a file with the header that is not listed, and on copies naming different commits.

`self update REV` writes every listed file at `REV` and runs the check. A shared file is edited here, never in a copy: a
copy that must change is changed in this repository and updated.
