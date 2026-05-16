# PRRT_kwDOSJAM6s6Ci7MN Plan

## Problem Statement and Scope

Address the unresolved review thread for `src/awf/runtime/ci_failure_evidence.py`
where fallback pytest repro commands can corrupt shell setup prefixes such as
`cd services/api && pytest` by quoting `&&` as a literal argument.

Scope is limited to CI failure evidence repro-command extraction and focused
unit coverage.

## Requirements Checklist

- Add a regression test showing a fallback command with shell setup before
  `pytest` produces an executable focused repro suggestion.
- Preserve existing behavior for ordinary pytest commands, including dropping
  broad original pytest arguments before appending focused node IDs.
- Avoid weakening existing regression tests or safety assertions.
- Run the narrow unit test file for CI failure evidence.

## Implementation Steps

1. Add the failing regression in `tests/unit/runtime/test_ci_failure_evidence.py`.
2. Update `_pytest_repro_command` to preserve the original command prefix up to
   `pytest` or `python -m pytest` instead of rebuilding that prefix with
   `shlex.join`.
3. Run the targeted test file and fix any regressions.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q`

Pass criteria: all tests in the targeted file pass, including the new fallback
shell-prefix regression.
