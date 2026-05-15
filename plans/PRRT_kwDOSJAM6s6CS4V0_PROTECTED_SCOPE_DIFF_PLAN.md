# PRRT_kwDOSJAM6s6CS4V0 Protected Scope Diff Plan

## Problem Statement and Scope

Remote-baseline verification failures in protected-scope repair are currently
collapsed into `None`, which is also used for ordinary "repair did not produce
committable changes" outcomes. This can let monitor recovery continue without a
distinct `PROTECTED_SCOPE_DIFF_UNAVAILABLE` failure.

Scope is limited to the protected-scope repair path in
`src/awf/runtime/pr_monitor_runner.py` and focused regression coverage in the
PR monitor unit tests.

## Requirements Checklist

- Add a regression test proving a protected-scope repair baseline failure is
  surfaced as `PROTECTED_SCOPE_DIFF_UNAVAILABLE`.
- Preserve fail-closed behavior: do not stage, commit, or push after the
  baseline verification fails.
- Keep successful protected-scope restore filtering behavior unchanged.
- Keep existing push-time protected-scope diff handling unchanged.

## Implementation Steps

1. Add or update focused unit coverage for the CI repair path and direct
   dirty-worktree protected revert check.
2. Change protected-scope remote restore filtering so fetch, merge-base, and
   diff failures raise `ProtectedScopeDiffError` instead of returning `None`.
3. Convert that exception at monitor recovery boundaries into a failed
   `_GitPushResult` with reason code `PROTECTED_SCOPE_DIFF_UNAVAILABLE`.
4. Re-run the targeted unit tests and relevant static checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: targeted tests pass, static checks pass, and changed behavior
returns a protected-scope diff-unavailable failure instead of continuing to
commit or push.
