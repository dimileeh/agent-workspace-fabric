# Review 4620252998 Plan

## Problem Statement And Scope

Address PR review comment `issue:4620252998` about preserved compose teardown
fallback for completed workspaces whose PR has not merged. The scoped risk is
that a successful fallback compose teardown for `COMPLETED_PR_NOT_MERGED` can
feed the side-effect workspace list and revoke secret leases or release resource
reservations for an active unmerged workspace.

The comment also notes that compose-first ordering can leave leases in place
when Docker teardown repeatedly fails. That behavior is an intentional safety
tradeoff in the current implementation, so this plan only documents the focused
check and does not broaden the change.

## Requirements Checklist

- Remove the `COMPLETED_PR_NOT_MERGED` preserved fallback path for compose
  teardown.
- Preserve fallback compose teardown for completed, merged workspaces that are
  still within retention.
- Add/update focused regression coverage proving unmerged completed workspaces
  do not trigger fallback teardown, secret lease revocation, or reservation
  release.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only.

## Implementation Steps

1. Update `src/awf/service/gc.py` so `_preserved_workspace_allows_compose_teardown_fallback`
   only permits the canonical completed merged within-retention fallback.
2. Update unit tests that currently expect unmerged completed workspaces to use
   fallback compose teardown.
3. Run a targeted pytest selection for the changed GC behavior.
4. Commit the scoped fix locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "unmerged or retained_merged or fallback_compose_teardown"`
  - Passes and covers the affected single-workspace fallback behavior.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
