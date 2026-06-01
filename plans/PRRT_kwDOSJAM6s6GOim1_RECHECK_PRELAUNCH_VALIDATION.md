# PRRT_kwDOSJAM6s6GOim1 Recheck Prelaunch Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GOim1_RECHECK_PRELAUNCH_PLAN.md`

## Requirement Status

- Add a regression test that fails before the implementation change:
  Complete.
- On `_recheck_before_launch()` exceptions after pre-launch metadata commit,
  mark the workspace failed without leaving `compose_project_name` set:
  Complete.
- Do not record `workspace.terminal_runtime_released` for this path because no
  containers were started:
  Complete.
- Preserve existing launched-stack failure behavior where
  `compose_project_name` remains set for cleanup:
  Complete.
- Run only focused local checks; full AWF/GitHub validation remains owned by AWF
  after agent completion:
  Complete.

## Evidence

Files changed:

- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py`
- `plans/PRRT_kwDOSJAM6s6GOim1_RECHECK_PRELAUNCH_PLAN.md`

Focused checks run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestRecheckBeforeLaunchFailure::test_recheck_exception_clears_prepublished_compose_project -q`
  failed because `reloaded.compose_project_name` was `awf_<workspace_id>`.
- Green check after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestRecheckBeforeLaunchFailure::test_recheck_exception_clears_prepublished_compose_project -q`
  passed.
- Focused provisioner regression file:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py -q`
  passed with `5 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py`
  passed.

Broad AWF/GitHub-owned validation was not run locally per the workspace
contract.
