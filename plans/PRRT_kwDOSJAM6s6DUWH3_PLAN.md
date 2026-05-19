# PRRT_kwDOSJAM6s6DUWH3 Plan

## Problem Statement

The PR monitor parses `git diff --name-status -z` output for protected-scope
classification. Reviewer feedback reports that malformed or truncated
NUL-delimited records currently return best-effort partial paths, which can drop
protected files and weaken the conservative quality gate.

## Scope

- Update the protected-scope diff parser path in
  `src/awf/runtime/pr_monitor_runner.py`.
- Add focused regression tests for malformed `--name-status -z` output.
- Keep existing successful parsing behavior for valid NUL-delimited output.

## Requirements

- [ ] Missing NUL delimiters from a `-z` diff are treated as malformed output.
- [ ] Truncated NUL-delimited records fail closed instead of returning partial
      paths.
- [ ] Parse failures surface as `ProtectedScopeDiffError` to the protected-scope
      caller.
- [ ] Valid NUL-delimited diff output still returns a deduplicated tuple of
      changed paths.

## Implementation Steps

1. Add failing regression tests around malformed `--name-status -z` output.
2. Update the parser/caller to fail closed on malformed output.
3. Adjust affected test fixtures to use valid NUL-delimited fake diff output.
4. Run the narrow unit tests that cover the touched behavior, then lint if time
   and environment permit.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
