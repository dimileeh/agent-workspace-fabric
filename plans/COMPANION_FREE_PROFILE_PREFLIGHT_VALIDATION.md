# Companion-Free Profile Preflight Validation

Plan reference: `plans/COMPANION_FREE_PROFILE_PREFLIGHT_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing a companion-free profile with an
  invalid service dependency graph fails before stack launch.
- Complete: The regression asserts no profile secret lease records are issued
  before the preflight failure.
- Complete: `Provisioner` now runs `validate_companion_service_graph` for
  profile services even when there are zero companion specs.
- Complete: Existing companion preflight behavior is preserved by validating
  before companion materialization and still marking the launch request
  prevalidated.
- Complete: Verification used focused commands only. Full AWF/GitHub validation
  is managed by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
- `plans/COMPANION_FREE_PROFILE_PREFLIGHT_PLAN.md`
- `plans/COMPANION_FREE_PROFILE_PREFLIGHT_VALIDATION.md`

Focused checks:

- Failing-first evidence: the new regression initially failed because stack
  launch was reached before provisioner preflight.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py::TestSuccess::test_rejects_profile_only_invalid_service_graph_before_secret_leases -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py::TestSuccess::test_rejects_invalid_companion_graph_before_materializing_companions -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py::TestSuccess::test_transitions_to_ready_only_after_stack_launch_succeeds tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py::TestSuccess::test_materializes_companion_worktrees_before_stack_launch -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
  passed.

## Remaining Gaps

No planned gaps remain. Broad validation, full coverage, and CI-equivalent
checks were intentionally not run in the agent phase per the AWF workspace
contract.
