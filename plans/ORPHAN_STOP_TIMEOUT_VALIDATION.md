# Orphan Stop Timeout Validation

Plan reference: `plans/ORPHAN_STOP_TIMEOUT_PLAN.md`

## Requirement Status

- Add a bounded per-operation timeout around orphan container stop during
  launch-lost-to-terminal cleanup: Complete.
- Preserve existing failure behavior by recording
  `orphan_containers_stopped=false`, `orphan_stop_error`, and a terminal runtime
  release revoke event on orphan stop failure: Complete.
- Add regression coverage proving a hung orphan stop does not hang the
  provisioner: Complete.
- Run focused validation only and leave broad AWF/GitHub validation to the
  post-agent workflow: Complete.

## Evidence

Files changed:

- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
- `plans/ORPHAN_STOP_TIMEOUT_PLAN.md`
- `plans/ORPHAN_STOP_TIMEOUT_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload -q`
  - First run failed before implementation because the provisioner hung until the
    test-level timeout.
  - Final run passed: `1 passed in 1.48s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q`
  - Passed after formatting: `19 passed in 22.33s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  - Passed: `All checks passed!`.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  - Passed: `2 files already formatted`.

Full AWF/GitHub validation was not run inside the agent phase per the workspace
contract; AWF owns broad validation after completion.

## Gaps

None.
