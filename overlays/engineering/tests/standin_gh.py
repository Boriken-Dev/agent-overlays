"""A stand-in for GitHub's `gh` that keeps issues and pull requests in the JSON file `STANDIN_STATE` names.

It answers only the subcommands the records provider uses and appends each call's arguments to `<state>.calls`, so a
test can see what was asked. It never touches the network.
"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["STANDIN_STATE"])


def load():
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"items": {"issue": [], "pr": []}, "labels": [], "next": 1}


def save(data):
    STATE.write_text(json.dumps(data), encoding="utf-8")


def flags(args):
    out, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in ("--add-label", "--remove-label", "--label"):
            out.setdefault(a, []).append(args[i + 1])
            i += 2
        elif a in ("--yes", "--force"):
            out[a] = True
            i += 1
        elif a.startswith("--"):
            out[a] = args[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return out, rest


def shape(it):
    return {
        "number": it["number"], "title": it["title"], "body": it["body"], "state": it["state"],
        "labels": [{"name": n} for n in it["labels"]], "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z", "url": "https://github.example/%s/issues/%d" % (it["repo"], it["number"]),
    }


def main():
    argv = sys.argv[1:]
    with open(str(STATE) + ".calls", "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    data = load()
    noun, verb = argv[0], argv[1]
    opts, rest = flags(argv[2:])
    repo = opts.get("--repo", "")
    if "--repo" not in opts:
        sys.stderr.write("no --repo\n")
        return 1
    if noun == "label":
        if rest[0] not in data["labels"]:
            data["labels"].append(rest[0])
        save(data)
        return 0
    items = data["items"][noun]
    if verb == "list":
        print(json.dumps([shape(i) for i in items][: int(opts.get("--limit", 30))]))
        return 0
    if verb == "create":
        body = sys.stdin.read()
        for lab in opts.get("--label", []):
            if lab not in data["labels"]:
                sys.stderr.write("label %s does not exist\n" % lab)
                return 1
        it = {"number": data["next"], "title": opts["--title"], "body": body, "state": "OPEN",
              "labels": list(opts.get("--label", [])), "repo": repo}
        data["next"] += 1
        items.append(it)
        save(data)
        print("https://github.example/%s/issues/%d" % (repo, it["number"]))
        return 0
    number = int(rest[0])
    found = [i for i in items if i["number"] == number]
    if not found:
        sys.stderr.write("could not resolve to an issue or pull request with the number of %d\n" % number)
        return 1
    it = found[0]
    if verb == "view":
        print(json.dumps(shape(it)))
    elif verb == "edit":
        for lab in opts.get("--add-label", []):
            if lab not in data["labels"]:
                sys.stderr.write("label %s does not exist\n" % lab)
                return 1
            it["labels"].append(lab)
        for lab in opts.get("--remove-label", []):
            it["labels"].remove(lab)
        if "--title" in opts:
            it["title"] = opts["--title"]
        if "--body-file" in opts:
            it["body"] = sys.stdin.read()
    elif verb == "close":
        it["state"] = "CLOSED"
    elif verb == "reopen":
        it["state"] = "OPEN"
    elif verb == "delete":
        items.remove(it)
    else:
        sys.stderr.write("unknown command %s %s\n" % (noun, verb))
        return 1
    save(data)
    return 0


sys.exit(main())
