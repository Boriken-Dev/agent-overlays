"""`configure-pages` -- set a repository's GitHub Pages docs-site settings to the project standard.

    configure-pages OWNER/REPO [--tag-pattern v*] [--branch main] [--dry-run]

A workflow can enable Pages and build and deploy the docs site, but three repository-level settings cannot be set
by the workflow itself, and they fail with errors that name a REST endpoint rather than the missing setting, so
they get missed on every new repository until a release run hits them. The engineering overlay's flows/REPO.md
("Hosted docs site") has the rationale; this command applies the settings it documents:

  1. Default workflow permissions matter only to a Pages job that does NOT declare `permissions: pages: write`
     (at job or workflow level): such a job runs with the repository default, and under "read" `configure-pages`
     fails with "Resource not accessible by integration". A job that declares it works under "read". So the default
     is changed to write only when a workflow has an undeclared Pages job; otherwise it is reported and left.
  2. Pages must exist with build_type=workflow (build from GitHub Actions, not "deploy from a branch").
  3. The `github-pages` environment's deployment-branch-policy must allow BOTH the release tag pattern and the
     default branch, or a deploy is rejected with "... is not allowed to deploy to github-pages due to environment
     protection rules" after the docs already built successfully, so it reads like a deploy bug.

     Both halves matter, and the second is easy to miss: GitHub's default is "only the default branch", so turning
     custom policies on to allow the tag REVOKES the branch unless it is added back explicitly. With the tag as the
     only policy, every docs run triggered by a push to the default branch builds the site and then fails at
     deploy, while the tag-dispatched runs succeed, so it reads as a trigger bug rather than a missing setting.

A failed step does not stop the run: every failure is listed at the end and the exit code is 1 if there was any.

Requires the `gh` CLI, authenticated, with admin rights on the repository.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

from duho import Cmd, LoggingArgs

FAILURES: "list[str]" = []


PAGES_ACTION = re.compile(r"uses:\s*['\"]?actions/(?:configure-pages|deploy-pages)@")
PAGES_WRITE = re.compile(r"\bpages\s*:\s*['\"]?write\b|\bwrite-all\b")


def gh(*args, dry_run=False, allow_fail=False):
    """Run gh; on failure return None, recording it unless allow_fail."""
    cmd = ["gh", *args]
    if dry_run and args[0] == "api" and "-X" in args and args[args.index("-X") + 1] in ("POST", "PUT", "PATCH", "DELETE"):
        print(f"  [dry-run] would run: {' '.join(cmd)}")
        return None
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if not allow_fail:
            err = result.stderr.strip()
            print(f"  [fail] {' '.join(cmd)}\n         {err}")
            FAILURES.append(f"{' '.join(cmd)}: {err}")
        return None
    return result.stdout.strip()


def get_json(*args, allow_fail=False):
    out = gh(*args, allow_fail=allow_fail)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _block(lines, start):
    """Lines indented deeper than lines[start], up to the next sibling."""
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return out


def _permissions(lines, indent):
    """The `permissions:` value at exactly `indent` among lines, or None if absent."""
    for i, line in enumerate(lines):
        if len(line) - len(line.lstrip()) == indent and line.lstrip().startswith("permissions:"):
            return line.split(":", 1)[1] + "\n" + "\n".join(_block(lines, i))
    return None


def pages_jobs(text):
    """(job, declares_pages_write, declares_any_permissions) per job using a Pages action.

    An indentation scan, not a YAML parse: enough for the workflows these
    repos ship, and it needs no third-party package.
    """
    lines = text.splitlines()
    top = _permissions(lines, 0)
    jobs_at = next((i for i, l in enumerate(lines) if l.startswith("jobs:")), None)
    if jobs_at is None:
        return []
    body = _block(lines, jobs_at)
    code = [l for l in body if l.strip() and not l.lstrip().startswith("#")]
    if not code:
        return []
    job_indent = min(len(l) - len(l.lstrip()) for l in code)
    found = []
    for i, line in enumerate(body):
        if line not in code or len(line) - len(line.lstrip()) != job_indent or not line.rstrip().endswith(":"):
            continue
        job = _block(body, i)
        if not PAGES_ACTION.search("\n".join(job)):
            continue
        child = min((len(l) - len(l.lstrip()) for l in job if l.strip() and not l.lstrip().startswith("#")),
                    default=job_indent + 2)
        perms = _permissions(job, child)
        if perms is None:
            perms = top
        found.append((line.strip().rstrip(":"), bool(perms and PAGES_WRITE.search(perms)), perms is not None))
    return found


def workflows_needing_write(repo):
    """Pages jobs that rely on the repo default token permissions, or None if unreadable."""
    listing = get_json("api", f"repos/{repo}/contents/.github/workflows", allow_fail=True)
    if not isinstance(listing, list):
        return None
    undeclared, broken = [], []
    for entry in listing:
        if not entry.get("name", "").endswith((".yml", ".yaml")):
            continue
        text = gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{entry['path']}")
        if text is None:
            continue
        for job, has_write, declares in pages_jobs(text):
            where = f"{entry['name']}:{job}"
            if has_write:
                continue
            (broken if declares else undeclared).append(where)
    return undeclared, broken


def run(repo, tag_pattern="v*", branch_arg="", dry=False):
    """Apply the three settings to `repo`; the exit code."""

    print(f"=== {repo} ===")

    # 1. Default workflow permissions: write only if a Pages job relies on the default.
    perms = get_json("api", f"repos/{repo}/actions/permissions/workflow")
    current = perms.get("default_workflow_permissions") if perms else None
    scan = workflows_needing_write(repo)
    if scan is None:
        print(f"[warn] could not read .github/workflows; default workflow permissions left at {current!r}")
    else:
        undeclared, broken = scan
        for where in broken:
            print(f"[warn] {where} declares permissions without `pages: write`; "
                  "no repo default can fix it, add `pages: write` to that job")
        if current == "write":
            print("[ok] default workflow permissions are read+write (left unchanged)")
        elif undeclared:
            print(f"[fix] default workflow permissions: {current!r} -> write "
                  f"(Pages job(s) without `permissions:`: {', '.join(undeclared)})")
            gh("api", "-X", "PUT", f"repos/{repo}/actions/permissions/workflow",
               "-f", "default_workflow_permissions=write",
               "-F", "can_approve_pull_request_reviews=false", dry_run=dry)
        else:
            print(f"[ok] default workflow permissions {current!r}: every Pages job declares "
                  "`pages: write`, so no change is needed")

    # 2. Pages enabled, building from GitHub Actions.
    pages = get_json("api", f"repos/{repo}/pages", allow_fail=True)
    if pages and pages.get("build_type") == "workflow":
        print("[ok] Pages already enabled (build_type=workflow)")
    elif pages:
        print(f"[fix] Pages build_type: {pages.get('build_type')!r} -> workflow")
        gh("api", "-X", "PUT", f"repos/{repo}/pages", "-f", "build_type=workflow", dry_run=dry)
    else:
        print("[fix] Pages not enabled -- enabling with build_type=workflow")
        gh("api", "-X", "POST", f"repos/{repo}/pages", "-f", "build_type=workflow", dry_run=dry)

    # 3. github-pages environment deployment-branch-policy must allow the tag pattern.
    env = get_json("api", f"repos/{repo}/environments/github-pages", allow_fail=True)
    policy = (env or {}).get("deployment_branch_policy") or {}
    if not policy.get("custom_branch_policies"):
        print("[fix] github-pages environment: enabling custom branch policies")
        gh("api", "-X", "PUT", f"repos/{repo}/environments/github-pages",
           "-f", "deployment_branch_policy[protected_branches]=false",
           "-f", "deployment_branch_policy[custom_branch_policies]=true", dry_run=dry)

    existing = get_json("api", f"repos/{repo}/environments/github-pages/deployment-branch-policies", allow_fail=True)
    allowed = {(b["name"], b.get("type")) for b in (existing or {}).get("branch_policies", [])}

    # The default branch deploys "latest" docs between releases. Enabling
    # custom policies above replaced GitHub's "only the default branch"
    # default with this explicit list, so it has to be named here or pushes
    # to it are rejected -- see the module docstring.
    branch = branch_arg or None
    if branch is None:
        info = get_json("api", f"repos/{repo}") or {}
        branch = info.get("default_branch") or "main"
    for name, kind in ((branch, "branch"), (tag_pattern, "tag")):
        if (name, kind) in allowed:
            print(f"[ok] {kind} policy {name!r} already allowed")
            continue
        print(f"[fix] adding {kind} deployment policy {name!r}")
        gh("api", "-X", "POST", f"repos/{repo}/environments/github-pages/deployment-branch-policies",
           "-f", f"name={name}", "-f", f"type={kind}", dry_run=dry)

    if FAILURES:
        print(f"{len(FAILURES)} step(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("done." if not dry else "dry-run complete -- rerun without --dry-run to apply.")
    return 0


class ConfigurePages(LoggingArgs, Cmd):
    """Configure a repository's GitHub Pages docs-site settings (workflow permissions, Pages, deploy policy)."""

    _parsername_ = "configure-pages"

    repo: str = ""
    "The repository, OWNER/REPO."
    ("repo",)

    tag_pattern: str = "v*"
    "Deployment tag pattern to allow."
    ("--tag-pattern",)

    branch: str = ""
    "Branch to allow deploying from (default: the repository's default branch)."
    ("--branch",)

    dry_run: bool = False
    "Print what would change without making requests."
    ("--dry-run",)

    def __call__(self) -> int:
        if "/" not in self.repo:
            print("error: give the repository as OWNER/REPO", file=sys.stderr)
            return 2
        FAILURES.clear()
        return run(self.repo, self.tag_pattern, self.branch, self.dry_run)


if __name__ == "__main__":
    from duho import main as _duho_main

    raise SystemExit(_duho_main(ConfigurePages))
