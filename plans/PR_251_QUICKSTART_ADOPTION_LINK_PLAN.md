# PR 251 Quickstart Adoption Link Plan

## Problem Statement And Scope

CI fails the docs regression test
`test_reference_docs_link_to_canonical_adoption_runbook` because
`docs/QUICKSTART.md` does not reference the canonical PR monitor adoption
runbook, while the other reference docs already do.

Scope is limited to restoring that Quickstart cross-link and verifying the
focused docs test.

## Requirements Checklist

- Add a `PR_MONITOR_ADOPTION.md` reference to `docs/QUICKSTART.md`.
- Preserve existing quickstart structure and operator flow.
- Do not disable, skip, or weaken the failing docs test.
- Verify with the focused failing pytest node.

## Implementation Steps

1. Add the PR monitor adoption runbook to the Quickstart "Next" links.
2. Rerun the focused pytest node from CI.
3. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_pr_monitor_adoption_docs.py::test_reference_docs_link_to_canonical_adoption_runbook -q
```

Passes when the focused pytest node exits successfully.
