# Review 4561562913 Companion Policy Plan

## Problem Statement And Scope

Review comment `issue:4561562913` identified two defensive gaps in
`src/awf/node/companion_services.py` when companion services are deserialized
from persisted task-policy documents instead of the validated API path:

- `_optional_int` ignores float values, so a manually edited
  `compose_up_timeout_seconds` can silently fall back to the profile default.
- `_companion_spec_from_mapping` accepts invalid `environment_secrets` target
  keys, so malformed policy JSON can fail later in Docker Compose instead of at
  AWF's parsing boundary.

The fix is limited to task-policy parsing behavior and unit coverage. No
GitHub writes, branch changes, pushes, or broad AWF/CI validation are in scope.

## Requirements Checklist

- Add a regression test showing float `compose_up_timeout_seconds` values are
  handled explicitly during task-policy deserialization.
- Add a regression test showing invalid `environment_secrets` target keys are
  rejected during task-policy deserialization.
- Preserve existing API validation behavior and existing environment-secret
  resolution behavior.
- Keep checks focused to the changed unit-test surface.
- Document validation evidence in `plans/review_4561562913_companion_policy_VALIDATION.md`.

## Implementation Steps

1. Add focused tests in `tests/unit/node/test_companion_services.py`.
2. Add Docker-compatible environment secret target-key validation in
   `src/awf/node/companion_services.py`.
3. Update `_optional_int` to handle floats intentionally instead of silently
   treating them as unsupported object types.
4. Run the narrow unit tests that cover companion service task-policy parsing.
5. Record validation status and evidence.
6. Stage and commit only the files changed for this review comment.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Pass criteria: the focused companion service unit-test file passes. Full
AWF/GitHub validation is intentionally left to AWF after agent completion.
