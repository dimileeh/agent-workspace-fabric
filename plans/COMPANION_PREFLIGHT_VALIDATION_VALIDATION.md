# Companion Preflight Validation

Plan reference: `plans/COMPANION_PREFLIGHT_VALIDATION_PLAN.md`

## Requirement Status

- Complete: Invalid companion/profile graphs fail with `ProfileResolutionError`
  before companion `add_worktree` calls.
- Complete: Provisioning marks the workspace failed with
  `profile_resolution_failure` for preflight graph validation errors.
- Complete: Existing successful companion materialization behavior remains
  unchanged.
- Complete: Preflight validation uses the same `validate_companion_service_graph`
  rules as the launcher.

## Evidence

Files changed:

- `src/awf/node/provisioner.py`
- `src/awf/node/companion_services.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "rejects_invalid_companion_graph"`:
  initially failed before the production change, then passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`:
  passed, 16 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "companion"`:
  passed, 2 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/node/companion_services.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`:
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/provisioner.py src/awf/node/companion_services.py`:
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this agent phase.

## Gaps

None.
