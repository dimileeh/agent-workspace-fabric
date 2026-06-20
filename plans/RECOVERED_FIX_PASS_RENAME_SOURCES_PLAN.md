# Recovered Fix-Pass Rename Sources Plan

## Problem Statement and Scope

An unresolved PR review thread reports that missing-HEAD pre-push validation recovery
diffs use `git diff --name-only -z`, which omits rename source paths. If a recovered
commit renames a protected file out of a protected path, the protected-scope check can
miss the protected source path.

Scope is limited to the recovered-head protected-scope diff path in
`src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` and focused
regression coverage for that path.

## Requirements Checklist

- Use the existing `--name-status -z` changed-path parser for recovered fix-pass diffs.
- Include both rename source and destination paths when calling the recovered
  protected-scope checker.
- Preserve fail-closed behavior for malformed or unavailable recovered diff output.
- Avoid broad validation; run only focused tests for the changed behavior.

## Implementation Steps

1. Add a regression test that simulates recovered diff output for a rename from
   `.github/workflows/ci.yml` to `docs/ci.yml`.
2. Confirm the test fails against the existing `--name-only` implementation when
   practical.
3. Change recovered-head diff collection from `--name-only -z` splitting to
   `--name-status -z` plus the existing parser.
4. Run the focused regression test and adjacent recovered-head edge tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head`

Pass criteria: the focused recovered-head tests pass, including the new rename-source
regression. Full AWF/GitHub validation is managed by AWF after agent completion.
