# Flow: Plan Generation

A plan is read on every executor pass: spend tokens on precision, not prose. Plans
are autonomous: resolve choices while planning, state defaults and observable
deviation triggers, and never defer a question to execution.

## Ready Contract

1. **No user input**: resolve open questions by research or an explicit default.
   Number simultaneous choices as `Design Q1`, `Q2`, with a bold `Decision:`.
2. **No implementation**: paths, signatures, hints, a one-line *Why*, and code
   blocks of at most five lines. Commands are complete literals, environment setup
   included; verify undocumented third-party behaviour against the real system.
3. **Required shape**: `plans add` makes it: an `executor` attribute (optional
   `priority`, `tags`, `links`, `depends_on`, set with `plans set`), then
   `# <Title>`, `## Progress`, `## Known Facts & Context`, `## Phases` (files,
   guidance, Why, observable done-when), `## Verification`, `## Reporting`.
4. **Right-sized**: split above ~150 lines or for independently executable phases
   with `plans add --complex`: one overall plan and a file per phase or extra.
   Executors read the overall plan and their assigned phase.
5. **Evidence**: performance baselines before claims, registry metadata before new
   dependencies, per-item consent for bundled approvals. A plan that breaks a
   documented API names its known consumers (dependents, sibling repos), and its
   Verification runs their suites against the new tree.

## Writing one

Run `plans add <name> -t "<title>" -p "<phase>" ...` (add `--complex` for a split
plan), then fill it with `plans edit`. `plans validate <name>` must pass before
`plans status <name> ready`.

Executor kickoff, given with the plan:

> Read `$ENGINEERING_OVERLAY_ROOT/flows/EXEC.md` and, if present,
> `$<LANG>_OVERLAY_ROOT/kb/<LANG>.md`. Execute plan `<name>` precisely; `EXEC.md`
> resolves its `Executor:` role and provider. Keep `## Progress` live (`[/]` when
> started, `[x]` when done); record blockers and continue independent work without
> asking the user.

## Status

Statuses: `draft`, `ready`, `review`, `executing`, `on-hold`, `done`.
`plans status <name> <status>` changes it (`done` requires every phase checked and
`-m` evidence; `on-hold` requires `-m` reason). `plans providers` lists the other
storage backends (`github`, `gitlab`, `jira`, `ai6`).

## Query, edit and remove

| Need | Command |
| --- | --- |
| what exists | `plans list` / `plans list --where "status = 'ready'"` |
| SQL search | `plans query "SELECT name, status, done, total, title FROM plans ..."` |
| what to do now | `plans next <name>`: attributes, progress, current phase; `--block`: only that phase's block |
| the phases | `plans phases <name>` (`--open`, `--format json`): id, state, title |
| one part | `plans show <name> --section facts` / `--phase 3` / `--brief` (frontmatter, title, progress) |
| attributes | `plans set <name> --attr key=val` / `--unset key` |
| rewrite a section | `plans edit <name> --section facts --set "..."` / `--set-file F` |
| add to a section | `plans edit <name> --section facts --append "..."` / `--append-file F` |
| change exact text | `plans edit <name> [--section S] --replace old=new` |
| add, rename, remove, split a phase | `plans phase-add <name> "title" [--after N]` / `phase-rename <name> N "title"` / `phase-remove <name> N [--force]` / `phase-split <name> N "A" "B"`: Progress line and block together, renumbered |
| a step | `plans start`, `check -m`, `block -m`, `uncheck` |
| a fact learned | `plans note <name> -m "..."`; on one phase: `plans note <name> --phase 3 -m "..."` |
| remove / migrate | `plans rm <name>` / `plans migrate` |
| other records | `records query|show|add|set|edit|mv|rm <folder>` |

Do not read a whole plan to learn one thing, or rewrite one to change a line.
`--body` / `--body-file` replace the entire plan and need `--whole`; to change one
section use `--section S --set`, never a whole-body file. Edit phases with the
`phase-*` commands, not by hand: they keep the Progress list and the blocks in step.

## Ready Checklist

- No user questions, guesses, incomplete commands, or non-observable done-when items.
- Known Facts holds discovered context; Verification runs setup before checks.
- Optional-test skip counts and expected outcomes are stated.
- No code block exceeds five lines.
- `executor` names a role present in `MODELS.md`; no raw provider IDs.
- A rule cited to skip, stub or shrink work is quoted verbatim with `file:line`,
  never paraphrased.
