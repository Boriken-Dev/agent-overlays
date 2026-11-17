# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The eight overlays `engineering`, `net`, `node`, `private-sync`, `python`, `recovery`, `release` and `rust`, as they stood on the
  `repo` branch of `jose-pr/dotagents`, which keeps their earlier history.
- `catalog.toml`, listing each overlay with its summary, icon, homepage and tags, for stores that read catalogs.
- An `[about]` table in each overlay's `overlay.cfg` (summary, icon, homepage, repository, license, tags) and an `icon.png`
  drawn by `tools/make_icons.py`. Nothing in them changes what an overlay does.
- `tools/validate.py` and the manual `Validate` workflow.
