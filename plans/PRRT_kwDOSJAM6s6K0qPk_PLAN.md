# PRRT_kwDOSJAM6s6K0qPk Plan

## Problem Statement and Scope

The missing-HEAD filesystem recovery path in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` returns the final
`git rev-parse HEAD` result without stripping inherited git object lookup
environment variables and without confirming that the recovered commit exists
in the shared mirror object database.

Scope is limited to this recovery helper and focused regression coverage for
the reviewed behavior.

## Requirements Checklist

- Final recovered `HEAD` lookup must not inherit `GIT_OBJECT_DIRECTORY` or
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`.
- A recovered SHA must be accepted only when the mirror can prove
  `<sha>^{commit}` with `git cat-file -e` under a sanitized object lookup
  environment.
- Failed final mirror verification must fail closed by returning `None`.
- Existing recovery behavior outside this verification path must remain
  unchanged.

## Implementation Steps

1. Add a focused failing test for the final recovered-SHA mirror verification
   and sanitized final `rev-parse`.
2. Update `_recover_missing_head_object_from_filesystem` to run the final
   `rev-parse HEAD` with sanitized object lookup environment.
3. Verify the recovered SHA with mirror `cat-file -e <sha>^{commit}` before
   returning it.
4. Update any existing focused tests that model the successful recovery command
   sequence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`

Pass criteria: the focused unit file passes. Full AWF/GitHub validation is
managed by AWF after agent completion and is intentionally not run here.
