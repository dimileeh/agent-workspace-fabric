# PRRT_kwDOSJAM6s6COa9S Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6COa9S` reports that setup dependency network
retry classification misses pip commands that put valid value-taking global
options before the dependency subcommand, for example
`pip --cache-dir /tmp/pip install -r requirements.txt` or
`pip --log /tmp/pip.log install ...`.

Scope is limited to dependency setup command parsing in
`src/awf/runtime/validation.py` and focused regression coverage in
`tests/unit/runtime/test_validation.py`. No PR comments, pushes, branch changes,
or broad refactors are in scope.

## Requirements Checklist

- Add regression coverage for pip value-taking global options before
  `install`, including the review examples.
- Update the dependency setup subcommand scanner so those option values are
  skipped before looking for `install`, `download`, or `wheel`.
- Preserve existing behavior for already-supported package manager options and
  transient dependency-network classification.
- Validate with the narrow runtime validation test surface.
- Commit the local fix with a conventional commit message tied to the review
  thread id.

## Implementation Steps

1. Extend the existing pip value-flag regression test and confirm the new cases
   fail before the code change.
2. Add the missing pip value-taking global options to the shared setup
   dependency option-value skip list.
3. Re-run the focused test, then the runtime validation unit test file.
4. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6COa9S_VALIDATION.md`.
5. Stage only changed files and create the local commit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_pip_value_flags_before_subcommand -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`

Pass criteria: both commands pass without weakening existing assertions.
