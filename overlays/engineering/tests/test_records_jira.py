"""The Jira provider against a loopback stand-in for Jira's REST API: no real Jira, no real token."""
from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

OVERLAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OVERLAY / "lib" / "python"))

import _records  # noqa: E402

STATUSES = ["To Do", "In Progress", "Done"]


class FakeJira(BaseHTTPRequestHandler):
    issues: dict
    auth_seen: list
    page = 2

    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def _shape(self, i):
        return {"key": i["key"], "fields": {"summary": i["summary"], "description": i["description"],
                                            "status": {"name": i["status"]}, "created": "2026-01-01T00:00:00.000+0000",
                                            "updated": "2026-01-02T00:00:00.000+0000"}}

    def _route(self, method):
        self.auth_seen.append(self.headers.get("Authorization"))
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        parts = url.path.strip("/").split("/")  # rest api 2 ...
        rest = parts[3:]
        keys = sorted(self.issues, key=lambda k: int(k.split("-")[1]))
        if rest[0] == "search":
            if "PRJ" not in q.get("jql", ""):
                return self._send(400, {"errorMessages": ["bad jql"]})
            if rest[-1] == "jql":
                start = int(q.get("nextPageToken") or 0)
                chunk = keys[start:start + self.page]
                out = {"issues": [self._shape(self.issues[k]) for k in chunk]}
                if start + self.page < len(keys):
                    out["nextPageToken"] = str(start + self.page)
                return self._send(200, out)
            start = int(q.get("startAt", 0))
            chunk = keys[start:start + self.page]
            return self._send(200, {"issues": [self._shape(self.issues[k]) for k in chunk], "total": len(keys)})
        if rest == ["issue"] and method == "POST":
            f = self._body()["fields"]
            key = "PRJ-%d" % (len(self.issues) + 1)
            self.issues[key] = {"key": key, "summary": f["summary"], "description": f["description"],
                                "status": "To Do", "type": f["issuetype"]["name"]}
            return self._send(201, {"key": key})
        key = rest[1]
        if key not in self.issues:
            return self._send(404, {"errorMessages": ["Issue does not exist"]})
        it = self.issues[key]
        if len(rest) == 3 and rest[2] == "transitions":
            if method == "GET":
                return self._send(200, {"transitions": [
                    {"id": str(i), "to": {"name": s}} for i, s in enumerate(STATUSES) if s != it["status"]]})
            it["status"] = STATUSES[int(self._body()["transition"]["id"])]
            return self._send(204)
        if method == "GET":
            return self._send(200, self._shape(it))
        if method == "PUT":
            f = self._body()["fields"]
            it["summary"], it["description"] = f["summary"], f["description"]
            return self._send(204)
        if method == "DELETE":
            del self.issues[key]
            return self._send(204)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def jira(tmp_path, monkeypatch):
    handler = type("H", (FakeJira,), {"issues": {}, "auth_seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok-123")
    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "records.toml").write_text(
        '[plans]\nprovider = "jira"\nurl = "http://127.0.0.1:%d"\nproject = "PRJ"\ntoken_env = "TEST_JIRA_TOKEN"\n'
        'user = "me@example.test"\ndefault_status = "draft"\n'
        '[plans.status_map]\ndraft = "To Do"\nexecuting = "In Progress"\ndone = "Done"\n' % server.server_port,
        encoding="utf-8",
    )
    prov = _records.open_provider(_records.FolderStore(agents / "plans"), agents, scope_key="plans")
    prov.fake = handler
    yield prov
    server.shutdown()
    server.server_close()


def test_create_get_query_update(jira):
    made = jira.create("Ship it", meta={"severity": "high"}, body="Details.\n")
    assert made["name"] == "PRJ-1" and made["title"] == "Ship it" and made["status"] == "draft"
    assert made["severity"] == "high" and made["body"].strip() == "Details."
    stored = jira.fake.issues["PRJ-1"]
    assert stored["type"] == "Task" and stored["status"] == "To Do"
    # The key and status are Jira's own, not repeated in the description.
    assert "name:" not in stored["description"] and "status:" not in stored["description"]
    assert stored["description"].startswith("{code:records}\n")
    assert jira.get("prj-1")["name"] == "PRJ-1"
    out = jira.update("PRJ-1", meta={"severity": "low"})
    assert out["severity"] == "low" and out["body"].strip() == "Details."
    assert [r["name"] for r in jira.query(where="severity = 'low'")[1]] == ["PRJ-1"]


def test_status_maps_to_workflow_state_by_transition(jira):
    jira.create("a")
    assert jira.move("PRJ-1", "executing")["status"] == "executing"
    assert jira.fake.issues["PRJ-1"]["status"] == "In Progress"
    assert jira.move("PRJ-1", "done")["status"] == "done"
    # A Jira status with no mapping reads as its own lower-cased name.
    jira.fake.issues["PRJ-1"]["status"] = "In Progress"
    jira.config["status_map"] = {}
    assert jira.get("PRJ-1")["status"] == "in progress"
    with pytest.raises(SystemExit, match="Jira offers"):
        jira.move("PRJ-1", "Nonexistent")


def test_created_in_a_mapped_status_is_transitioned(jira):
    jira.create("a", meta={"status": "done"})
    assert jira.fake.issues["PRJ-1"]["status"] == "Done"


def test_listing_pages_in_both_flavors(jira):
    for i in range(5):
        jira.create("t%d" % i)
    assert [r["name"] for r in jira.list_records()] == ["PRJ-%d" % i for i in range(1, 6)]
    jira.config["flavor"] = "server"
    assert len(jira.list_records()) == 5
    assert len(jira.list_records(status="draft")) == 5 and jira.list_records(status="done") == []


def test_delete_missing_and_closed(jira):
    jira.create("a", meta={"status": "done"})
    with pytest.raises(SystemExit, match="pass --force"):
        jira.delete("PRJ-1")
    jira.delete("PRJ-1", force=True)
    with pytest.raises(SystemExit, match="HTTP 404"):
        jira.get("PRJ-1")


def test_credentials_come_from_the_named_variable_only(jira, monkeypatch):
    jira.list_records()
    expected = "Basic " + base64.b64encode(b"me@example.test:tok-123").decode()
    assert set(jira.fake.auth_seen) == {expected}
    assert "tok-123" not in json.dumps(jira.capabilities())
    jira.config.pop("user")
    jira.list_records()
    assert jira.fake.auth_seen[-1] == "Bearer tok-123"
    monkeypatch.delenv("TEST_JIRA_TOKEN")
    with pytest.raises(SystemExit, match="TEST_JIRA_TOKEN"):
        jira.list_records()


def test_an_unreachable_server_is_a_clear_failure():
    prov = _records.JiraProvider({"url": "http://127.0.0.1:1", "project": "PRJ", "token_env": "PATH"})
    with pytest.raises(SystemExit, match="jira http://127.0.0.1:1"):
        prov.list_records()
