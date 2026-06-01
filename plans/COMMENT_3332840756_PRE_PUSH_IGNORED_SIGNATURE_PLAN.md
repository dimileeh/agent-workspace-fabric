# Comment 3332840756 Pre-Push Ignored Signature Plan

## Context

PR review thread `PRRT_kwDOSJAM6s6GDo7z` reports that
`_pre_push_validation_new_ignored_entries` only compares ignored snapshot
signatures when both baseline and current signature tuples are non-empty. That
can miss drift when the ignored snapshot path set is unchanged but only one side
has a content signature for an ignored path.

## Plan

1. Add a focused regression test beside the existing pre-push ignored-entry
   helper tests that demonstrates unchanged ignored paths with a one-sided
   signature tuple are treated as drift.
2. Update `_pre_push_validation_new_ignored_entries` to compare signature maps
   whenever either side captured signatures, matching the executor validation
   drift behavior.
3. Run only focused tests for the changed runtime pre-push validation behavior.
4. Record focused validation evidence here in the companion validation document.

## Scope Boundaries

- Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
  validation.
- Preserve existing safety tests and assertions.
