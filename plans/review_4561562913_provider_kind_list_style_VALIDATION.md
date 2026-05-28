# Review 4561562913 Provider Kind List Style Validation

Plan reference:
`plans/review_4561562913_provider_kind_list_style_PLAN.md`

## Requirement Status

- Complete: Added regression coverage showing explicit invalid or falsy
  `provider` and `kind` values are rejected at task-policy parsing time.
- Complete: Preserved the existing default of `provider=env` and `kind=env`
  when those fields are omitted.
- Complete: Added regression coverage showing list-style Compose
  `environment` sections remain list-style after optional env-secret removal
  empties the list and restore adds a present optional placeholder.
- Complete: Kept checks focused to the touched unit-test surfaces.
- Complete: Documented validation evidence in this file.

## Files Changed

- `src/awf/node/companion_services.py`
- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/node/test_companion_services.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `plans/review_4561562913_provider_kind_list_style_PLAN.md`
- `plans/review_4561562913_provider_kind_list_style_VALIDATION.md`

## Evidence

Initial failing regression check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_rejects_unsupported_environment_secret_scope_fields tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_companion_env_secret_refresh_preserves_list_environment_format_when_restoring_after_emptying -q
```

Result: failed as expected with 8 `DID NOT RAISE` failures for unsupported
`provider`/`kind` values and one list-vs-dict assertion failure for resume YAML
refresh.

Final focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: 55 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "companion_env_secret_refresh or restore_compose_environment_list_refs"
```

Result: 4 passed, 13 deselected.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py src/awf/control/executor/monitor_handoff.py tests/unit/node/test_companion_services.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py
```

Result: all checks passed.

Full AWF/GitHub validation was not run during the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
