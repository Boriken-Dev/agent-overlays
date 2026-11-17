# agent-overlays: repository orientation

A collection of Agent Overlay Specification (AOS) overlays and the catalog that lists them. It ships nothing as a package: a
consumer reads folders and `catalog.toml` straight from the repository.

## Layout

| Path | Responsibility |
| --- | --- |
| `overlays/<name>/` | One overlay: `overlay.cfg`, its `icon.png`, and the knowledge, commands, templates and tests it brings. Its own `AGENTS.md` or `kb/` describes it |
| `catalog.toml` | The catalog: one full entry per overlay, with a relative source and the `about` fields a store shows |
| `tools/make_icons.py` | Draws every `icon.png` (standard library only, deterministic); `--check` fails when one is stale |
| `tools/validate.py` | Reads every overlay and the catalog with coqui-aos and checks the catalog agrees with each overlay's own `[about]` |
| `.github/workflows/validate.yml` | Runs both tools; started by hand only |

## Rules

- A folder name is the overlay's `name`, and `catalog.toml` lists exactly the folders under `overlays/`.
- Each overlay's `[about]` (summary, icon, homepage, tags) is repeated in its catalog entry, and `tools/validate.py` fails when
  they differ. Tags come from the list in `tools/validate.py`.
- An icon is a 256 by 256 PNG made by `tools/make_icons.py`; edit the script, never the PNG.
- Public text only: no private paths, hostnames, names or plans in an overlay. No executables, key files or opaque binaries.
- LF line endings everywhere (`.gitattributes`).
- No tag without the owner's say-so; the repository has no releases yet.

## Commands

```sh
pip install "coqui-aos @ git+https://github.com/Boriken-Dev/coqui-aos#subdirectory=python"
python tools/validate.py
python tools/make_icons.py --check
```
