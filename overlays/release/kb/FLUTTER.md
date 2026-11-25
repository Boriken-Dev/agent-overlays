# Flutter mirror, SDK and images

`flutter/` holds the tools that let a Flutter SDK be built from Boriken Dev's own mirrors, with nothing fetched from Google's storage or
from `flutter/flutter` while a project builds. Run each with `$AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/<file>` or its launcher
(`flutter-mirror`, `flutter-artifacts`, `flutter-sdk`). They are standard library only and need `git`, and `gh` where they publish.

| File | Launcher | What it does |
| --- | --- | --- |
| `flutter_mirror.py` | `flutter-mirror` | Keeps a scanner-clean mirror of Flutter's `stable` and `master` branches and their tags in `Boriken-Dev/flutter`: fetches, removes what a malware or archive scanner refuses (checked with the engineering overlay's `blob-scan` skill, which it loads by path), rewrites history to match, pushes, and adds the one workflow file `flutter_images_dispatch.yml` that starts the image build. The mirror's commits are made under the machine's own `git config user.name` and `user.email`, which its SHAs depend on |
| `flutter_artifacts.py` | `flutter-artifacts` | `list`, `publish` and `fetch-tree` the engine artifacts of one engine hash as release assets of `Boriken-Dev/flutter` with a SHA-256 manifest, and `serve` them on loopback as the storage the Flutter tool downloads from (`FLUTTER_STORAGE_BASE_URL`) |
| `flutter_sdk.py` | `flutter-sdk` | `install` an SDK from the mirrors, `update` its pin to the newest upstream commit whose engine is fully published, `verify` it on a Canoa checkout with only the mirrors reachable, and `image web`, `linux` or `windows` to build and push the container images |
| `flutter_logproxy.py` | none | A logging proxy to Google's storage: run Flutter with `FLUTTER_STORAGE_BASE_URL` pointing at it to see which files a command downloads |
| `flutter_images_dispatch.yml` | none | The workflow that `flutter_mirror.py` commits to the mirror's default branch so a build can be started by hand |

Each file's docstring is its reference (`--help` prints it). `flutter_sdk.py verify` and the `Pin for Canoa` line of `update` are Canoa's:
they run Canoa's own build steps. The rest has no project in it.

Defaults: the mirror's local state is `~/.cache/flutter_mirror` (`--state`, or `$FLUTTER_MIRROR_STATE`), the served tree is `~/.cache/flutter_artifacts/tree`
(`--tree`/`--dir`), downloads are staged in `~/.cache/flutter_artifacts/work/<engine>` (`--work`).
