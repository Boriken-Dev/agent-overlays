"""`configure-pages`: the three settings, in order, with every failure listed and a dry run that changes
nothing. `gh` is a recording stand-in."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools_env import no_machine_agent_files  # noqa: F401  (an autouse fixture)

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "skills" / "configure-pages" / "configure_pages.py"
_spec = importlib.util.spec_from_file_location("configure_pages_cmd", MODULE)
pages = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pages
_spec.loader.exec_module(pages)

DECLARED = "name: docs\non: push\njobs:\n  deploy:\n    permissions:\n      pages: write\n    steps:\n      - uses: actions/deploy-pages@v4\n"
UNDECLARED = "name: docs\non: push\njobs:\n  deploy:\n    runs-on: x\n    steps:\n      - uses: actions/configure-pages@v5\n"
BROKEN = "name: docs\non: push\njobs:\n  deploy:\n    permissions:\n      contents: read\n    steps:\n      - uses: actions/deploy-pages@v4\n"


class FakeGh:
    """Answers `gh api` reads from a table and records every call."""

    def __init__(self, workflow=DECLARED, permissions="read", pages=None, env=None, policies=(), fail=()):
        self.calls = []
        self.workflow, self.permissions, self.pages, self.env = workflow, permissions, pages, env
        self.policies, self.fail = list(policies), list(fail)

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        out, code = "", 0
        endpoint = next((a for a in args if a.startswith("repos/")), "")
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if method == "GET":
            if endpoint.endswith("actions/permissions/workflow"):
                out = json.dumps({"default_workflow_permissions": self.permissions})
            elif endpoint.endswith("contents/.github/workflows"):
                out = json.dumps([{"name": "docs.yml", "path": ".github/workflows/docs.yml"}])
            elif endpoint.endswith("docs.yml"):
                out = self.workflow
            elif endpoint.endswith("/pages"):
                code, out = (0, json.dumps(self.pages)) if self.pages else (1, "")
            elif endpoint.endswith("deployment-branch-policies"):
                out = json.dumps({"branch_policies": [{"name": n, "type": t} for n, t in self.policies]})
            elif endpoint.endswith("github-pages"):
                code, out = (0, json.dumps(self.env)) if self.env is not None else (1, "")
            else:
                out = json.dumps({"default_branch": "trunk"})
        return subprocess.CompletedProcess(cmd, code, out, "" if code == 0 else "not found")

    def writes(self):
        return [c for c in self.calls if "-X" in c and c[c.index("-X") + 1] != "GET"]


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        gh = FakeGh(**kw)
        monkeypatch.setattr(pages.subprocess, "run", gh)
        return gh
    return install


def run(*flags, repo="o/r"):
    return duho.parse(pages.ConfigurePages, [repo, *flags])()


def endpoint(call):
    return next(a for a in call if a.startswith("repos/"))


def method(call):
    return call[call.index("-X") + 1]


def test_a_fresh_repository_gets_pages_the_environment_and_both_policies(fake, capsys):
    gh = fake()
    assert run() == 0
    out = capsys.readouterr().out
    assert "[fix] Pages not enabled" in out and "enabling custom branch policies" in out
    assert "adding branch deployment policy 'trunk'" in out and "adding tag deployment policy 'v*'" in out
    made = [(method(c), endpoint(c)) for c in gh.writes()]
    assert ("POST", "repos/o/r/pages") in made
    assert ("PUT", "repos/o/r/environments/github-pages") in made
    assert made.count(("POST", "repos/o/r/environments/github-pages/deployment-branch-policies")) == 2
    assert not any("actions/permissions/workflow" in e for _, e in made)  # the job declares pages: write


def test_an_undeclared_pages_job_raises_the_default_to_write(fake, capsys):
    gh = fake(workflow=UNDECLARED)
    run()
    assert any("actions/permissions/workflow" in endpoint(c) for c in gh.writes())
    assert "default workflow permissions: 'read' -> write" in capsys.readouterr().out


def test_a_job_with_permissions_but_no_pages_write_is_warned_about(fake, capsys):
    gh = fake(workflow=BROKEN)
    run()
    assert "declares permissions without `pages: write`" in capsys.readouterr().out
    assert not any("actions/permissions/workflow" in endpoint(c) for c in gh.writes())


def test_everything_in_place_changes_nothing(fake, capsys):
    gh = fake(pages={"build_type": "workflow"}, env={"deployment_branch_policy": {"custom_branch_policies": True}},
              policies=[("trunk", "branch"), ("v*", "tag")])
    assert run() == 0
    assert gh.writes() == []
    assert "done." in capsys.readouterr().out


def test_a_branch_build_type_is_changed_to_workflow(fake):
    gh = fake(pages={"build_type": "legacy"}, env={"deployment_branch_policy": {"custom_branch_policies": True}},
              policies=[("trunk", "branch"), ("v*", "tag")])
    run()
    puts = [c for c in gh.writes() if method(c) == "PUT"]
    assert len(puts) == 1 and puts[0][-2:] == ["-f", "build_type=workflow"]


def test_the_branch_and_tag_flags_are_used(fake):
    gh = fake(pages={"build_type": "workflow"}, env={"deployment_branch_policy": {"custom_branch_policies": True}})
    run("--branch", "dev", "--tag-pattern", "rel-*")
    names = [c[c.index("-f") + 1] for c in gh.writes()]
    assert "name=dev" in names and "name=rel-*" in names


def test_dry_run_makes_no_write(fake, capsys):
    gh = fake()
    assert run("--dry-run") == 0
    assert gh.writes() == []
    out = capsys.readouterr().out
    assert "[dry-run] would run" in out and "dry-run complete" in out


def test_a_failed_step_is_listed_and_the_rest_still_run(fake, capsys):
    fake(fail=["name=trunk"])
    assert run() == 1
    out = capsys.readouterr().out
    assert "1 step(s) failed" in out and "adding tag deployment policy 'v*'" in out


def test_the_failure_list_does_not_carry_over_between_runs(fake):
    fake(fail=["name=trunk"])
    assert run() == 1
    fake()
    assert run() == 0


def test_a_repository_without_an_owner_is_a_usage_error(fake, capsys):
    fake()
    assert run(repo="norepo") == 2
