---
name: records
description: Query, add, update, edit, move, and delete frontmatter-Markdown records across any folder (findings, plans, decisions, kb) using read-only SQL and pluggable providers.
---

# records

Use `records` (`skills/records/records.py`) to run SQL queries (`SELECT ... FROM records WHERE ...`), inspect sections, or mutate YAML frontmatter attributes across any `.agents/<folder>` store.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/records/records.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `records` directly.

## Records in a tracker

By default a record is a Markdown file. `<project>/.agents/records.toml` can name another home for a folder, keyed by the
folder's name (`plans`, `findings`, ...); every command (`query`, `show`, `add`, `set`, `edit`, `mv`, `rm`) then runs against it.

- The record's **name is the tracker's key** (an issue number, a Jira key) and its **status is where it stands in the tracker**
  (a label, or a workflow state). Neither is stored a second time. `add` takes a title (the name you give) and prints the key
  the tracker assigned; use that key afterwards.
- The title, dates and address are the tracker's own fields. The other frontmatter fields (`severity`, `tags`, ...) are kept in
  a block at the top of the issue's text and read back as columns for `query`; the body is the rest of the text.
- Credentials are never written to a file: GitHub and GitLab use the person's own `gh` / `glab` login; Jira reads the
  environment variable `token_env` names.

```toml
[plans]
provider = "github"
repo = "owner/name"
# kind = "pr"                   # pull requests instead of issues (they can be listed, edited and moved, not created or deleted)
# status_prefix = "status:"     # a record's status is the label status:<status>
# default_status = "active"     # status of an issue with no status label
# closed_statuses = ["done", "processed"]   # moving to one of these closes the issue; moving out reopens it
# command = "gh"                # the program, when it is not `gh` on PATH
```

GitLab is the same through `glab` (its REST API, so the person's `glab auth login` is the credential); `kind = "mr"` makes the
records merge requests:

```toml
[findings]
provider = "gitlab"
project = "group/name"      # a path with its groups
```

Jira reads one environment variable for its credential and moves a record by the issue's workflow transition whose target is
the status `status_map` names; a Jira status with no entry reads as its own lower-cased name. With `user` (or `user_env`) the
token is an API token for basic authentication, without it a bearer token. `flavor = "server"` uses the older search path
of Jira Server and Data Center.

```toml
[plans]
provider = "jira"
url = "https://example.atlassian.net"
project = "PRJ"
token_env = "JIRA_API_TOKEN"     # the NAME of the variable that holds the token; never the token
user = "me@example.com"          # or user_env = "JIRA_USER"
# issue_type = "Task"
default_status = "draft"
[plans.status_map]
draft = "To Do"
executing = "In Progress"
done = "Done"
```

The Jira fields block is a `{code:records}` block at the top of the description, so it shows as code in Jira.
