# Review 4561562913 Provider Kind List Style Plan

## Problem Statement And Scope

Review comment `issue:4561562913` identifies two remaining defensive and
round-trip consistency issues in the companion env-secret work:

- Persisted `environment_secrets` entries can explicitly set unsupported falsy
  `provider` or `kind` values such as `null`, `false`, or `0` and still be
  normalized to `env` during task-policy deserialization.
- Monitor-resume optional env-secret refresh can change a Compose service's
  `environment` section from list style to mapping style when list entries are
  removed and a different optional secret is restored in the same pass.

Scope is limited to companion secret task-policy parsing, the resume YAML
refresh helpers, focused regression coverage, and this plan/validation record.
No GitHub writes, branch changes, pushes, or broad AWF/CI validation are in
scope.

## Requirements Checklist

- Add regression coverage showing explicit invalid or falsy `provider` and
  `kind` values are rejected at task-policy parsing time.
- Preserve the existing default of `provider=env` and `kind=env` when those
  fields are omitted.
- Add regression coverage showing list-style Compose `environment` sections
  remain list-style after optional env-secret removal empties the list and
  restore adds a present optional placeholder.
- Keep checks focused to the touched unit-test surfaces.
- Document validation evidence in
  `plans/review_4561562913_provider_kind_list_style_VALIDATION.md`.

## Implementation Steps

1. Add focused failing tests in `tests/unit/node/test_companion_services.py`
   and
   `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`.
2. Update `_environment_secret_ref` in `src/awf/node/companion_services.py` to
   default omitted `provider`/`kind` to `env` while rejecting explicit
   unsupported values.
3. Update resume YAML environment-target removal in
   `src/awf/control/executor/monitor_handoff.py` so list-style environment
   sections remain lists when all entries are removed.
4. Run focused regression tests and focused lint for the touched files.
5. Record validation status and evidence.
6. Stage and commit only files changed for this review comment.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_rejects_unsupported_environment_secret_scope_fields tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_companion_env_secret_refresh_preserves_list_environment_format_when_restoring_after_emptying -q
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "companion_env_secret_refresh or restore_compose_environment_list_refs"
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py src/awf/control/executor/monitor_handoff.py tests/unit/node/test_companion_services.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py
```

Pass criteria: the focused regression tests pass, the existing companion-service
and resume-refresh focused unit surfaces pass, and ruff reports no issues for
the edited Python files. Full AWF/GitHub validation is intentionally left to
AWF after agent completion.
