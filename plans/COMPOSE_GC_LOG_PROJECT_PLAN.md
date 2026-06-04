# Compose GC Log Project Plan

## Problem Statement

PR review comment `issue:4620252998` reports that monitor compose teardown logs can emit
the runner-supplied `compose_project` instead of the DB-persisted project name when a
completed workspace is preserved by GC policy and compose teardown runs through the
single-workspace fallback path.

## Scope

- Keep the change limited to GC preserved metadata and monitor compose teardown logging.
- Do not alter teardown ordering, filesystem deletion, lease revocation, reservation
  release, or merge-monitor control flow.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only.

## Requirements

- Add a regression proving preserved fallback compose teardown log events prefer the
  persisted compose project name over the monitor fallback value.
- Preserve existing fallback behavior when no persisted compose project name exists.
- Keep GC plan serialization compatible for existing preserved entries.
- Commit the fix locally with a review-comment-specific conventional commit message.

## Implementation Steps

1. Extend preserved GC plan entries to carry optional compose metadata from the workspace
   row.
2. Populate that metadata in preserved classification paths.
3. Update monitor compose teardown log project resolution to check candidates and
   preserved entries before falling back to the runner argument.
4. Add focused runtime/service tests covering the corrected log behavior and metadata
   propagation.

## Verification

- Run targeted pytest for the affected runtime GC monitor test.
- Run targeted pytest for the affected service GC metadata test.
- Record that full AWF/GitHub validation is managed after agent completion.
