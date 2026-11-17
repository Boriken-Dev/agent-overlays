# agent-overlays

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Boriken-Dev/agent-overlays/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Boriken-Dev/agent-overlays/validate.yml)](https://github.com/Boriken-Dev/agent-overlays/actions/workflows/validate.yml)

**Overlays for coding agents, with a catalog a store can list.** Each folder under `overlays/` is an overlay in the
[Agent Overlay Specification](https://github.com/Boriken-Dev/agent-overlay-spec) format: working rules, workflows, templates
and commands an agent reads when you install it. Nothing here runs until you install an overlay and allow what it asks for.

## Features

- **Eight overlays** for engineering process, Node, Python and Rust conventions, HTTP tooling, release, private sync and recovery.
- **A catalog** (`catalog.toml`) with a summary, icon, homepage and tags for each, which Canoa's Store and any store that reads
  AOS catalogs lists.
- **Plain folders**: `dotagents` installs a folder straight from this repository, with no registry in between.

## Installation

With [dotagents](https://github.com/jose-pr/dotagents), name the folder `overlays` as the repository's catalog:

```bash
dotagents overlays add engineering --repo https://github.com/Boriken-Dev/agent-overlays.git@main#overlays
```

In [Canoa](https://github.com/Boriken-Dev/canoa), add the catalog under `[catalogs]` in `canoa.cfg`, then open Store, Overlays,
Discover:

```toml
[catalogs]
boriken = "https://github.com/Boriken-Dev/agent-overlays.git@main#catalog.toml"
```

Any other consumer of the specification takes the same two forms: the `overlays` folder (each subfolder is an overlay) or
`catalog.toml`.

## Overlays

| Overlay | Does | Tags |
| --- | --- | --- |
| [`engineering`](overlays/engineering) | Process rules, plan and review workflows, repository templates and the commands `plans`, `leak-check`, `eol`, `summarize-run`, `compare-bench` | workflow, process |
| [`net`](overlays/net) | A drop-in curl and an operating-system trust store for agents that must fetch URLs | network |
| [`node`](overlays/node) | Node and JavaScript conventions, a `package.json` template, CI workflows | language, javascript |
| [`private-sync`](overlays/private-sync) | Each project's agent files in one private git repository, synced between machines | sync, workflow |
| [`python`](overlays/python) | Python conventions and library standards, templates, `pyvenv` and `py` | language, python |
| [`recovery`](overlays/recovery) | Rebuild a lost or corrupted agent configuration from transcripts and caches | recovery |
| [`release`](overlays/release) | Cut releases and watch CI on GitHub or GitLab, with a commit plan | release, workflow |
| [`rust`](overlays/rust) | Rust conventions, crate standards, a `Cargo.toml` template, CI workflows | language, rust |

`node`, `python` and `rust` require `engineering`; install it with them.

## Development

```bash
pip install "coqui-aos @ git+https://github.com/Boriken-Dev/coqui-aos#subdirectory=python"
python tools/validate.py          # every overlay and the catalog
python tools/make_icons.py --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for adding an overlay.

## License

MIT, see [LICENSE](LICENSE).
