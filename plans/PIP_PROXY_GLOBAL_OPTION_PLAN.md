# Pip Proxy Global Option Plan

## Problem Statement and Scope

An unresolved PR review thread reports that setup dependency network retry
classification misses pip commands that pass the value-taking global `--proxy`
option before the `install` subcommand, for example
`pip --proxy http://proxy:8080 install -r requirements.txt`.

Scope is limited to dependency setup command parsing in
`src/awf/runtime/validation.py` and a focused regression test in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Add a regression proving pip global `--proxy <url>` before `install` is
  classified as a setup dependency network failure.
- Preserve existing behavior for package manager option parsing and transient
  failure classification.
- Keep the fix narrow and compatible with existing value-taking option parsing.
- Validate with the narrow runtime validation test surface.

## Implementation Steps

1. Add a failing regression test for `pip --proxy http://proxy:8080 install`.
2. Update dependency setup option parsing to skip pip's value-taking global
   `--proxy` option before the subcommand.
3. Run the focused test, then the runtime validation unit test file.
4. Record plan validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`

Pass criteria: the focused regression and the full runtime validation unit file
pass without weakening existing tests.
