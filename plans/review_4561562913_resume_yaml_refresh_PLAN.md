# review_4561562913_resume_yaml_refresh_PLAN

## Problem Statement And Scope

Address PR review comment `issue:4561562913` for optional companion env-secret
refresh during PR-monitor resume. Scope is limited to the resume compose YAML
refresh helpers, focused unit coverage, and this plan/validation record.

## Requirements Checklist

- Add an explicit maintainer note that the resume YAML read-modify-write path
  uses PyYAML and may drop comments or reformat block scalars when optional
  env-secret presence changes.
- Fix duplicate list-form environment keys so restore observability counts
  distinct logical secret targets, not every duplicated YAML list occurrence.
- Preserve existing behavior that all duplicate list entries are normalized to
  the same Compose placeholder.
- Add focused regression coverage for the duplicate list-key count.
- Do not run AWF/GitHub-owned broad validation; use narrow local checks only.

## Implementation Steps

1. Add a failing unit test for `_restore_compose_environment_list_refs` with
   duplicate target entries, asserting all duplicates are rewritten while the
   return count is one distinct restored target.
2. Update `_restore_compose_environment_list_refs` to count a target at most
   once during the update loop while still updating every duplicate occurrence.
3. Add the PyYAML round-trip trade-off comment near the live compose-file
   mutation path.
4. Run the focused unit test and a narrow lint check for the touched files.
5. Record validation evidence in
   `plans/review_4561562913_resume_yaml_refresh_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "companion_env_secret_refresh or restore_compose_environment_list_refs"`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  must pass.
- Full AWF/GitHub validation is intentionally not run during the agent phase.
