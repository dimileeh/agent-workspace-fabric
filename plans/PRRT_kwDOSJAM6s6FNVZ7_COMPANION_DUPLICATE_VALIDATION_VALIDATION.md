# Duplicate Companion Graph Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FNVZ7_COMPANION_DUPLICATE_VALIDATION_PLAN.md`

## Requirement Status

- Complete: Provisioner requests with companions mark the companion graph as
  prevalidated after the pre-materialization check succeeds.
- Complete: `ComposeStackLauncher` skips companion graph validation only when
  the request has materialized companions and the prevalidated marker is set.
- Complete: Default direct `ComposeStackLauncher` calls still validate profile
  and companion service graph errors.
- Complete: Validation stayed focused; full AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `src/awf/node/provisioner.py`
- `tests/unit/node/test_stack_launcher.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
- `plans/PRRT_kwDOSJAM6s6FNVZ7_COMPANION_DUPLICATE_VALIDATION_PLAN.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q -k "prevalidated"`:
  initially failed before implementation, then passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "materializes_companion_worktrees_before_stack_launch"`:
  initially failed before implementation, then passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q -k "prevalidated or preflights_profile_dependencies_without_companions or rejects_companion_profile_service_collision"`:
  passed, 3 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "materializes_companion_worktrees_before_stack_launch or rejects_invalid_companion_graph_before_materializing_companions"`:
  passed, 2 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py src/awf/node/provisioner.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`:
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/stack_launcher.py src/awf/node/provisioner.py`:
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this agent phase.

## Gaps

None.
