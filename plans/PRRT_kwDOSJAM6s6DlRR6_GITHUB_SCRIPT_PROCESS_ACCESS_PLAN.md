# PRRT_kwDOSJAM6s6DlRR6 GitHub Script Process Access Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DlRR6` reports that the
`actions/github-script` comment/notify safety check blocks `process.env` but
does not block equivalent bracket access such as `process['env']`. That allows
an informational comment step to read runtime secrets while still calling an
allowed GitHub comment API.

Scope is limited to the `actions/github-script` safety predicate in
`src/awf/control/quality_gates.py`, focused unit coverage in
`tests/unit/control/test_quality_gates.py`, and this plan/validation pair.

## Requirements Checklist

- Add a regression test showing a comment-labeled `actions/github-script`
  step that reads `process['env']` is blocked.
- Keep existing safe GitHub comment scripts admitted.
- Keep the existing fail-closed behavior for other unsafe script access.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Add the failing regression to the existing unsafe `github-script` input
   coverage.
2. Run the targeted test and confirm it fails before the production fix.
3. Update the blocked-access pattern to reject bracketed `process` access.
4. Re-run focused quality-gate tests and lint/type checks for touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  fails before the fix for the new regression and passes after the fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passes.
