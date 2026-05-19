# Review 4482045018 Summary Cleanup Plan

## Problem Statement and Scope

Greptile's review-level summary for PR comment `issue:4482045018` flagged two
cleanup items after the initial env-seeding fix:

- `_resolve_init_env_paths()` keeps a fallback branch that is only reachable
  when tests or stubs bypass the production `get_bootstrap_asset_root()`
  validation.
- Several `awf init` tests manually concatenate `result.stdout` with
  `getattr(result, "stderr", "")` when they intend to inspect combined CLI
  terminal output.

Scope is limited to comments and test assertion cleanup around `awf init`.

## Requirements Checklist

- Document why the compose-service guard remains even though production asset
  root resolution already validates the compose service file.
- Replace manual stdout/stderr concatenation in `tests/unit/cli/test_init.py`
  with `result.output` for combined CLI-output assertions.
- Preserve existing behavior and assertions; do not weaken warning or error
  expectations.
- Validate the focused init test file and lint the touched Python files.

## Implementation Steps

1. Add a short source comment above the compose-service guard.
2. Update affected init tests to assign combined output from `result.output`.
3. Run `tests/unit/cli/test_init.py`.
4. Run Ruff on the touched source and test files.
5. Record validation evidence in
   `plans/REVIEW_4482045018_SUMMARY_CLEANUP_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`

Pass criteria: all tests pass and Ruff reports no issues.
