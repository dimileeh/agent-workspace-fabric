# REVIEW_4404104728 Validation

Plan reference: `plans/REVIEW_4404104728_PLAN.md`

## Requirement Status

- Complete: Added a focused regression test proving `DOCKER_UNAVAILABLE` from
  launch-time companion image revalidation is mapped to
  `WorkspaceServiceExecutionError`.
- Complete: Kept adjacent compose-up mapping behavior unchanged; existing
  focused mapping tests still pass.
- Complete: Made the smallest production change in
  `src/awf/node/stack_launcher.py` by moving revalidation into the existing
  `try/except ComposeOperationError` block.
- Complete: Treated the quoted companion image missing-detection nitpick as
  stale for this branch. `_is_missing_image_inspect_failure` is in
  `src/awf/node/compose_manager.py` and already returns true only for
  `"no such image"`.
- Complete: Ran only focused local validation. Full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py`
- `plans/REVIEW_4404104728_PLAN.md`
- `plans/REVIEW_4404104728_VALIDATION.md`

Focused checks:

- Before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py::test_compose_stack_launcher_maps_revalidation_docker_unavailable -q`
  - Result: failed because `ComposeOperationError` escaped from
    `_revalidate_prebuilt_companion_images`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py::test_compose_stack_launcher_maps_revalidation_docker_unavailable -q`
  - Result: passed.
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py::test_compose_stack_launcher_fails_when_docker_missing_without_required_services tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py::test_compose_stack_launcher_reports_required_services_when_docker_missing tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py::test_compose_stack_launcher_reraises_non_docker_unavailable_errors -q`
  - Result: passed.

## Gaps

None.
