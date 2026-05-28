# Review 4561562913 Value From Validation Plan

## Problem Statement And Scope

Review comment `issue:4561562913` identifies a remaining defensive gap in
`src/awf/node/companion_services.py`: task-policy deserialization validates
`environment_secrets` target keys but accepts `value_from` without checking it
against the Docker-compatible environment variable name pattern. A direct DB
write or future non-API path could therefore build malformed Compose
interpolation such as `${BAD}NAME}`.

The fix is limited to companion environment-secret task-policy parsing and
focused unit coverage. No GitHub writes, branch changes, pushes, or broad
AWF/CI validation are in scope.

## Requirements Checklist

- Add a regression test showing invalid `environment_secrets.value_from` values
  are rejected during task-policy deserialization.
- Validate `value_from` with the same Docker-compatible environment variable
  pattern used for environment-secret target keys.
- Preserve existing valid `environment_secrets` deserialization behavior.
- Keep checks focused to the changed companion-service unit-test surface.
- Document validation evidence in
  `plans/review_4561562913_value_from_VALIDATION.md`.

## Implementation Steps

1. Add a focused failing test in `tests/unit/node/test_companion_services.py`
   for an invalid `value_from`.
2. Update `_environment_secret_ref` in
   `src/awf/node/companion_services.py` to reject invalid `value_from` values.
3. Run the narrow regression test and the companion-service unit file.
4. Record validation status and evidence.
5. Stage and commit only the files changed for this follow-up review fix.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_rejects_invalid_environment_secret_value_from -q
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
```

Pass criteria: the focused regression and companion service unit-test file pass,
and ruff reports no issues for the edited Python files. Full AWF/GitHub
validation is intentionally left to AWF after agent completion.
