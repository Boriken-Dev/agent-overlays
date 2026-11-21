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
3. **Required shape**: YAML frontmatter (`name`, `title`, `status`, `executor`, plus
   optional attributes like `priority`, `tags`, `links`, `depends_on`), followed by
   `# <Title>`, `## Progress`, `## Known Facts & Context`, `## Phases` (files,
   guidance, Why, observable done-when), `## Verification`, `## Reporting`.
4. **Right-sized**: split above ~150 lines or for independently executable phases
   using a complex plan directory (`--complex`): `plans/<status>/<name>/README.md`
   for the overall plan and `<phase|extra>.md` beside it. Executors read `README.md`
   and their assigned phase file.
5. **Evidence**: performance baselines before claims, registry metadata before new
   dependencies, per-item consent for bundled approvals. A plan that breaks a
   documented API names its known consumers (dependents, sibling repos), and its
   Verification runs their suites against the new tree.

## Writing one

Start with `plans add <name> -t "<title>" -p "<phase>" ...`: it writes
`$ENGINEERING_OVERLAY_ROOT/references/plan_template.md` into `plans/draft/`
(or `plans/draft/<name>/README.md` with `--complex`). `plans validate
<name>` must pass before the plan is set `ready`.

Executor kickoff, given with the plan:

> Read `$ENGINEERING_OVERLAY_ROOT/flows/EXEC.md` and, if present,
> `$<LANG>_OVERLAY_ROOT/kb/<LANG>.md`. Execute `<path>` precisely; `EXEC.md` resolves
> its `Executor:` role and provider. Keep `## Progress` live (`[/]` when started,
> `[x]` when done); record blockers and continue independent work without asking the
> user.

## Persistence & Layout

Plans live in `<project>/.agents/plans/<status>/`:

- `draft/<name>.md` (`status: draft`)
- `ready/<name>.md` (`status: ready`)
- `review/<name>.md` (`status: review`)
- `executing/<name>.md` (`status: executing`)
- `on-hold/<name>.md` (`status: on-hold`)
- `completed/<name>.md` (`status: done`)

Complex plans replace `<name>.md` with a directory `<name>/README.md` +
`<phases|extras>.md`. No `INDEX.md` is kept. `plans status <name> <status>`
updates the frontmatter `status` and moves the file or directory into its folder
(`done` requires every phase checked and `-m` evidence; `on-hold` requires `-m`
reason). Other storage backends (`github`, `gitlab`, `jira`, `ai6`) can be wired
via `.agents/records.toml` (`plans providers`).

## Query, edit and remove

| Need | Command |
| --- | --- |
| what exists | `plans list` / `plans list --where "status = 'ready'"` |
| SQL search | `plans query "SELECT name, status, done, total, title FROM plans ..."` |
| what to do now | `plans next <name>`: frontmatter head, progress, current phase |
| one part | `plans show <name> --section facts` / `--phase 3` |
| attributes | `plans set <name> --attr key=val` / `--unset key` |
| body / section | `plans edit <name> --section facts --append "..."` / `--replace old=new` |
| a step | `plans start`, `check -m`, `block -m`, `uncheck` |
| a fact learned | `plans note <name> -m "..."` |
| remove / migrate | `plans rm <name>` / `plans migrate` |
| arbitrary folder | `records query|show|add|set|edit|mv|rm <folder>` |

Do not read a whole plan to learn one thing, or rewrite one to change a line.

## Ready Checklist

- No user questions, guesses, incomplete commands, or non-observable done-when items.
- Known Facts holds discovered context; Verification runs setup before checks.
- Optional-test skip counts and expected outcomes are stated.
- No code block exceeds five lines.
- `executor` in frontmatter names a role present in `MODELS.md`; no raw provider IDs.
- A rule cited to skip, stub or shrink work is quoted verbatim with `file:line`,
  never paraphrased.
