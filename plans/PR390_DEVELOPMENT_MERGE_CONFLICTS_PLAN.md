# PR390 Development Merge Conflicts Plan

## Problem Statement And Scope

Resolve the in-progress merge of `origin/development` into the current AWF-managed
branch for PR #390. Scope is limited to the six files currently marked
unmerged, plus this plan and the matching validation document required by the
repository workflow.

## Requirements Checklist

- Resolve conflicts in `docs/GETTING_STARTED.md` while preserving useful content
  from both sides and favoring `origin/development` semantics where choices
  conflict.
- Resolve conflicts in `docs/MCP_SETUP.md` with the same preservation rule.
- Resolve conflicts in `docs/PROJECT_ONBOARDING.md` with the same preservation
  rule.
- Resolve conflicts in `docs/QUICKSTART.md` with the same preservation rule.
- Resolve conflicts in `tests/unit/cli/test_init_parts/test_init_part_004.py`.
- Resolve conflicts in `tests/unit/docs/test_public_docs_status.py`.
- Ensure no conflict markers remain in the resolved files.
- Run focused validation only; broad AWF/GitHub validation remains owned by AWF
  after agent completion.
- Stage the resolved files and commit locally with a conventional commit message.

## Implementation Steps

1. Inspect conflict hunks and compare HEAD vs `origin/development` intent.
2. Edit each conflicted file to integrate both sides, avoiding unrelated rewrites.
3. Run targeted checks for conflict markers and affected tests when practical.
4. Create `plans/PR390_DEVELOPMENT_MERGE_CONFLICTS_VALIDATION.md` with evidence.
5. Stage the resolved files and commit the merge resolution locally.

## Verification Commands And Pass Criteria

- `rg -n "^(<<<<<<<|=======|>>>>>>>)" <resolved files>`
  - Passes when it returns no matches.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py tests/unit/docs/test_public_docs_status.py -q`
  - Passes when the affected targeted test files pass. If unavailable or too
    broad for the workspace, document the blocker in validation.
