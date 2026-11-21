---
name: <name>
title: <Plan Title>
status: draft
executor: "<family/subrole from MODELS.md> — <one line: why this role>"
---

<!-- `plans add <name>` copies this file to
     <project>/.agents/plans/<status>/<snake_case_name>.md (or
     <project>/.agents/plans/<status>/<snake_case_name>/README.md with --complex)
     and removes these comments; fill every <placeholder>. This skeleton IS the
     required shape from $ENGINEERING_OVERLAY_ROOT/flows/PLAN.md — do not add,
     drop, or reorder sections. -->

# <Plan Title>

## Progress

<!-- [ ] pending · [/] active (set BEFORE writing code) · [x] done — short outcome ·
     [!] blocked — reason. One line per phase, `Phase <id>: <name>`; `plans
     start|check|block` change it. The box update is part of the step itself —
     never batched at the end. -->

- [ ] Phase 1: <name>
- [ ] Phase 2: <name>

## Known Facts & Context

<!-- Discovered context and evidence, not guesses: baselines before perf claims,
     registry metadata before new deps, verified third-party behavior. -->

- <fact>

## Phases

<!-- No implementation: paths, signatures, hints, code blocks ≤5 lines.
     Done-when must be observable. Split into <name>/README.md + <phase>.md above ~150 lines. -->

### Phase 1: <name>

- Files: <paths>
- Guidance: <signatures, hints, defaults; Design Q1 … **Decision:** … if choices exist>
- Why: <one line>
- Done-when: <observable check>

## Verification

<!-- Complete literal commands including environment setup; expected outcomes and
     skip counts. Unrun checks are "implemented, unverified", never done. -->

- <command> → <expected>

## Reporting

- Handoff includes: changed files, evidence per done-when, unverified items,
  skip counts, dirty state, resolved provider/model/settings.
