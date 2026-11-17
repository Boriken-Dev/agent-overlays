# Contributing

## Changing an overlay

Edit its files under `overlays/<name>/`. Anything an agent reads must be public text: no private paths, hostnames, personal
names or plans. Run the checks before you open a pull request:

```bash
pip install "coqui-aos @ git+https://github.com/Boriken-Dev/coqui-aos#subdirectory=python"
python tools/validate.py
python tools/make_icons.py --check
```

## Adding an overlay

1. Make `overlays/<name>/` with an `overlay.cfg` (`name` equal to the folder, `description`, `requires`, `routing`).
2. Add an `[about]` table to it: `summary` (one line), `icon = "icon.png"`, `homepage` (the folder's address on GitHub),
   `repository`, `license` and `tags`.
3. Add a glyph and a colour for it to `tools/make_icons.py` and run the script; commit the PNG it writes.
4. Add the overlay to `catalog.toml`, repeating the `summary`, `homepage` and `tags` of its `[about]` and pointing `icon` at
   `overlays/<name>/icon.png`.
5. Run `tools/validate.py`.

Tags come from one short list, kept in `tools/validate.py`: `language`, `workflow`, `sync`, `recovery`, `release`, `network`,
`process`, and one for the language an overlay is about (`javascript`, `python`, `rust`). Add a tag there only when no
existing one fits.

## Commits

One logical change per commit, written `type: description` (`feat:`, `fix:`, `docs:`, `chore:`). Update `CHANGELOG.md` for
anything a user of an overlay would notice.

## Line endings and files

Text files use LF. Commit text and images only: no executables, archives, keys or other binaries.
