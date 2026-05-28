# Review 4566042208 H03/H04 Traceability Plan

## Problem Statement And Scope

PR review comment `issue:4566042208` reports that
`TODO/awf-full-installer-first-run-setup-backlog.md` records H03 and H04 in
the locked human decisions prose but omits them from the auditable task backlog
and Wave 1 human gate tables. H04 is a preflight prerequisite for launching the
first implementation workspaces, so operators following only the schedule could
miss it.

Scope is limited to the planning/backlog artifact and the plan/validation files
required by this repository workflow.

## Requirements Checklist

- Add H03 and H04 to the Task Backlog table as `human` entries with
  `done - locked` status.
- Add task-card traceability for H03 and H04, including what each decision
  blocks.
- Make H04 visible in the Human Gate Before Wave 1 section.
- Keep the existing task graph and implementation task scope intact.
- Do not change runtime code or tests.

## Implementation Steps

1. Patch the Task Backlog table to include H03/H04.
2. Add H03/H04 human task cards after H02.
3. Update the Human Gate Before Wave 1 section to list H03/H04 and describe the
   H04 launch-preflight prerequisite.
4. Run focused text inspection to verify the scheduling doc now has the
   expected H03/H04 references.

## Verification Commands And Pass Criteria

```bash
rg -n "H03|H04|Human Gate Before Wave 1|Task Backlog" TODO/awf-full-installer-first-run-setup-backlog.md
sed -n '79,180p' TODO/awf-full-installer-first-run-setup-backlog.md
sed -n '912,955p' TODO/awf-full-installer-first-run-setup-backlog.md
```

Pass criteria:

- H03 and H04 appear in the Task Backlog table.
- H03 and H04 have human task cards with blocks relationships.
- H04 appears in the Wave 1 human gate table before T01/T02 launch guidance.
