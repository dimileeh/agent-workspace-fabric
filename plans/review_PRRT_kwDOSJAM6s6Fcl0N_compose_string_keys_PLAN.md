# Review PRRT_kwDOSJAM6s6Fcl0N Compose String Keys Plan

## Problem Statement And Scope

PR review feedback reports that optional companion env-secret refresh during
monitor resume loads persisted Compose YAML with `yaml.safe_load`. PyYAML uses
YAML 1.1 boolean coercion, so unquoted service names such as `on`, `off`, `yes`,
or `no` can become boolean mapping keys. The refresh helper then cannot find
the string service name from companion policy and a later write can persist the
service as `true` or `false`.

Scope is limited to the monitor resume compose YAML refresh path in
`src/awf/control/executor/monitor_handoff.py`, targeted unit coverage, and this
plan/validation record.

## Requirements Checklist

- Preserve existing optional companion env-secret omission/restoration behavior.
- Preserve scalar Compose mapping keys as strings while loading YAML for resume
  refresh, including service names that are YAML 1.1 boolean words.
- Avoid writing raw secret values to the refreshed Compose file.
- Add focused regression coverage for a service named `on`.
- Do not run AWF/GitHub-owned broad validation; use narrow local checks only.

## Implementation Steps

1. Add a failing unit test with a Compose service named `on` and an optional
   env-secret target that should be removed when its source env var is absent.
2. Update the resume YAML load path to use a safe PyYAML loader that constructs
   scalar mapping keys from their literal YAML text while preserving normal value
   construction.
3. Run the focused test first to confirm the regression fails before the code
   change when practical.
4. Run the targeted unit test selection and narrow lint check for changed files.
5. Record validation evidence in
   `plans/review_PRRT_kwDOSJAM6s6Fcl0N_compose_string_keys_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "companion_env_secret_refresh_preserves_yaml_boolean_service_name_as_string"`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "companion_env_secret_refresh or restore_compose_environment_list_refs"`
  must pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  must pass.
- Full AWF/GitHub validation is intentionally not run during the agent phase.
