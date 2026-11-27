"""Run private-sync's link, sync or background sync from this overlay's own library, by path.

    python -I run_link.py link PROJECT AGENTS_DIR
    python -I run_link.py sync PROJECT AGENTS_DIR MESSAGE
    python -I run_link.py turn-end PROJECT AGENTS_DIR MESSAGE
    python -I run_link.py runner AGENTS_DIR [--env-file FILE]

The hooks use this instead of the `private-sync` script: a hook is the program that must not
run what the agent wrote, so it loads nothing from the project's own `.agents`. The libraries are
`../lib/python/_link.py` and `_bgsync.py` beside this file's directory; nothing else is imported
from outside the standard library. `turn-end` records the request and starts `runner` detached
(see `_bgsync.py`); `link` also reports a failed background sync and asks for maintenance.
"""
import importlib.util
import logging
import sys
from pathlib import Path


def load(name="_link", key="private_sync_link"):
    source = Path(__file__).resolve().parent.parent / "lib" / "python" / (name + ".py")
    spec = importlib.util.spec_from_file_location(key, str(source))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv):
    if len(argv) < 3 or argv[1] not in ("link", "sync", "turn-end", "runner"):
        sys.stderr.write("usage: run_link.py link|sync|turn-end PROJECT AGENTS_DIR [MESSAGE]\n"
                         "       run_link.py runner AGENTS_DIR [--env-file FILE]\n")
        return 2
    mode = argv[1]
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    log = logging.getLogger("private-sync")
    link = load()
    if mode == "runner":
        env_file = argv[4] if len(argv) > 4 and argv[3] == "--env-file" else None
        return load("_bgsync", "private_sync_bg").runner(argv[2], link, env_file=env_file)
    if len(argv) < 4:
        sys.stderr.write("usage: run_link.py link|sync|turn-end PROJECT AGENTS_DIR [MESSAGE]\n")
        return 2
    project, agents = argv[2], argv[3]
    message = argv[4] if len(argv) > 4 else "sync"
    if mode == "link":
        link.link_project(project, agents, logger=log)
        load("_bgsync", "private_sync_bg").session_start(agents, Path(__file__).resolve(), log.info)
        return 0
    if mode == "turn-end":
        return load("_bgsync", "private_sync_bg").turn_end(
            agents, project, message, link, Path(__file__).resolve(), log.info)
    return link.sync_agents(agents, message=message, project_dir=project, logger=log)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
