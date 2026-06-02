# PRRT_kwDOSJAM6s6GbCqf Compose Launch Signal Validation

## Plan Reference

- `plans/PRRT_kwDOSJAM6s6GbCqf_COMPOSE_LAUNCH_SIGNAL_PLAN.md`

## Requirement Status

- Add a launcher-to-provisioner signal that fires only when the real compose-up
  attempt starts: Complete.
- Drive unexpected and `ComposeOperationError` failure cleanup from that signal:
  Complete.
- Add regression coverage for a pre-compose launcher failure after the
  pre-launch metadata commit: Complete.
- Update existing compose-failure tests to explicitly model an attempted
  compose-up: Complete.
- Record focused validation commands and pass criteria: Complete.

## Evidence

Changed files:

- `src/awf/node/compose_manager.py`
- `src/awf/node/stack_launcher.py`
- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `tests/unit/node/test_stack_launcher_edge_cases.py`
- `tests/unit/node/test_stack_launcher_parts/_helpers.py`
- `tests/unit/runtime/test_workspace_services_compose.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py -q`
  - First run before implementation failed with the new pre-compose assertions.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py -q`
  - Passed: 12 tests.
  - Final rerun after targeted `ruff format src/awf/node/provisioner.py` passed:
    12 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_edge_cases.py -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py -q`
  - Passed: 23 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  - Passed: 18 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q`
  - Passed: 13 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts -q`
  - Passed: 36 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_workspace_services_compose.py -q`
  - Passed: 11 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_edge_cases.py tests/unit/node/test_stack_launcher_parts -q`
  - Final rerun after the one-shot callback ordering tweak passed: 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/node/stack_launcher.py src/awf/node/compose_manager.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py tests/unit/node/test_stack_launcher_edge_cases.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_stack_launcher_parts/_helpers.py tests/unit/runtime/test_workspace_services_compose.py`
  - Passed, then rerun after the one-shot callback ordering tweak and passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/provisioner.py src/awf/node/stack_launcher.py src/awf/node/compose_manager.py`
  - Passed, then rerun after the one-shot callback ordering tweak and passed.

Full AWF/GitHub validation was not run inside this agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after agent completion.

## Gaps

No planned gaps remain.
