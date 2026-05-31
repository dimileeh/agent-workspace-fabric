# Comment 3329422788 Owned Paths TypeError Plan

## Scope

Address review thread `PRRT_kwDOSJAM6s6F5ujQ` on
`src/awf/runtime/pr_monitor_runner/comments.py`, which reports that
`_owned_paths_for_prompt()` silently returns no owned paths when the monitor
session factory raises `TypeError`.

## Plan

1. Add a focused regression test showing that a production `TypeError` from the
   session factory propagates instead of rendering prompts with empty
   `owned_paths`.
2. Update the comments runner so `_owned_paths_for_prompt()` no longer catches
   `TypeError` from `session_factory()`.
3. Adjust any test-only call sites that relied on an `object()` session factory
   by giving them an explicit stub or fixture.
4. Run the narrow tests covering the changed behavior. Full AWF/GitHub
   validation remains owned by AWF after agent completion.
5. Record validation evidence in a matching validation document and commit the
   scoped fix locally.
