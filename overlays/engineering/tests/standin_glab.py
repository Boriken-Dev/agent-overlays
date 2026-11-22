"""A stand-in for GitLab's `glab api` that keeps issues and merge requests in the JSON file `STANDIN_STATE` names.

It answers the REST paths the records provider uses and appends each call's arguments to `<state>.calls`. It never
touches the network.
"""
import json
import os
import sys
import urllib.parse
from pathlib import Path

STATE = Path(os.environ["STANDIN_STATE"])


def load():
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"issues": [], "merge_requests": [], "next": 1}


def fail(message):
    sys.stderr.write(message + "\n")
    return 1


def main():
    argv = sys.argv[1:]
    with open(str(STATE) + ".calls", "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    if argv[0] != "api":
        return fail("unknown command")
    target, method = argv[1], argv[argv.index("-X") + 1]
    payload = json.loads(sys.stdin.read()) if "--input" in argv else {}
    path, _, query = target.partition("?")
    parts = path.split("/")
    if parts[0] != "projects" or parts[2] not in ("issues", "merge_requests"):
        return fail("404 Not Found")
    if urllib.parse.unquote(parts[1]) != "g/p":
        return fail("404 Project Not Found")
    kind = parts[2]
    data = load()
    items = data[kind]
    q = dict(urllib.parse.parse_qsl(query))

    def shape(it):
        return {"iid": it["iid"], "title": it["title"], "description": it["description"], "state": it["state"],
                "labels": it["labels"], "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
                "web_url": "https://gitlab.example/g/p/-/%s/%d" % (kind, it["iid"])}

    if len(parts) == 3:
        if method == "GET":
            size, page = int(q.get("per_page", 20)), int(q.get("page", 1))
            print(json.dumps([shape(i) for i in items[(page - 1) * size: page * size]]))
            return 0
        if method == "POST":
            it = {"iid": data["next"], "title": payload["title"], "description": payload.get("description", ""),
                  "state": "opened", "labels": [l for l in payload.get("labels", "").split(",") if l]}
            data["next"] += 1
            items.append(it)
            STATE.write_text(json.dumps(data), encoding="utf-8")
            print(json.dumps(shape(it)))
            return 0
    iid = int(parts[3])
    found = [i for i in items if i["iid"] == iid]
    if not found:
        return fail("404 Not found")
    it = found[0]
    if method == "GET":
        print(json.dumps(shape(it)))
        return 0
    if method == "DELETE":
        items.remove(it)
    elif method == "PUT":
        if "title" in payload:
            it["title"] = payload["title"]
        if "description" in payload:
            it["description"] = payload["description"]
        for l in payload.get("remove_labels", "").split(","):
            if l in it["labels"]:
                it["labels"].remove(l)
        for l in payload.get("add_labels", "").split(","):
            if l and l not in it["labels"]:
                it["labels"].append(l)
        event = payload.get("state_event")
        if event:
            it["state"] = "closed" if event == "close" else "opened"
        print(json.dumps(shape(it)))
    STATE.write_text(json.dumps(data), encoding="utf-8")
    return 0


sys.exit(main())
