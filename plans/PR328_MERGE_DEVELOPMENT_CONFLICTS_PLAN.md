# PR328 Merge Development Conflicts Plan

## Problem Statement and Scope

Resolve the stopped merge from `origin/development` into the current AWF-managed PR #328 branch without switching branches or pushing. Scope is limited to the reported conflicts in:

- `docs/REASON_CATALOG.md`
- `src/awf/service/pr_monitor_adoption.py`

## Requirements Checklist

- Preserve the PR branch's `DUPLICATE_HOST_PORT` reason catalog entry.
- Preserve the base branch's `FORGE_NOT_SUPPORTED` reason catalog entry.
- Preserve the PR branch's effective worker node id behavior in PR monitor adoption resource reservations.
- Preserve the base branch's inline profile normalization and forge-gating behavior in PR monitor adoption.
- Remove all merge conflict markers.
- Stage the resolved files and commit locally with a conventional merge message.
- Run only focused local checks; AWF/GitHub own full validation after agent completion.

## Implementation Steps

1. Inspect each conflicted hunk and compare both sides.
2. Resolve `docs/REASON_CATALOG.md` by keeping both reason sections in catalog order.
3. Resolve `src/awf/service/pr_monitor_adoption.py` by keeping both imports needed by the already-merged implementation body.
4. Search touched files for remaining conflict markers.
5. Run focused syntax/test checks for the touched service behavior and docs catalog references.
6. Record validation evidence in `plans/PR328_MERGE_DEVELOPMENT_CONFLICTS_VALIDATION.md`.
7. Stage all merge-resolution files and commit locally.

## Verification Commands and Pass Criteria

- `rg -n '(<{7}|={7}|>{7})' docs/REASON_CATALOG.md src/awf/service/pr_monitor_adoption.py plans/PR328_MERGE_DEVELOPMENT_CONFLICTS_PLAN.md plans/PR328_MERGE_DEVELOPMENT_CONFLICTS_VALIDATION.md` returns no matches.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/pr_monitor_adoption.py` passes.
- Focused PR monitor adoption unit tests covering forge/profile behavior and exercising the PR branch node-id reservation path pass.
- `git status --short` shows no unmerged paths before commit.
- Full AWF/GitHub validation is not run locally and is left to AWF after agent completion.
