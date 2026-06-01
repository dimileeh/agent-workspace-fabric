# Address PRRT_kwDOSJAM6s6GPTGa Validation

Plan reference: `ADDRESS_THREAD_PRRT_kwDOSJAM6s6GPTGa_PLAN.md`

## Requirement Status

- Complete: Add a regression test for retry-time
  `ComposeOperationError(reason_code="DOCKER_UNAVAILABLE")` revalidation mapping.
  - Evidence: `tests/unit/node/test_stack_launcher_companion_images.py` adds
    `test_retry_revalidation_maps_docker_unavailable_to_workspace_error`.
  - TDD evidence: the new test failed before implementation with a raw
    `ComposeOperationError` from retry revalidation.

- Complete: Preserve missing-image retry behavior and remaining-image
  revalidation.
  - Evidence: existing retry tests in
    `tests/unit/node/test_stack_launcher_companion_images.py` continue to pass.

- Complete: Map retry-time Docker-unavailable revalidation failures through the
  workspace service error helper.
  - Evidence: `src/awf/node/stack_launcher.py` assigns `spec = retry_spec`
    before entering the retry `try` block, then wraps retry revalidation and
    retry compose-up in the same `ComposeOperationError` handler.

- Complete: Keep changes scoped.
  - Evidence: changed files are limited to stack launcher implementation,
    focused stack launcher tests, and plan/validation artifacts.

- Complete: Run focused validation only.
  - Evidence: see commands below. Full AWF/GitHub validation is managed by AWF
    after agent completion per the workspace contract.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_retry_revalidation_maps_docker_unavailable_to_workspace_error -q`
  - Before implementation: failed with raw `ComposeOperationError`.
  - After implementation: passed.

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  - Passed: `15 passed`.

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py -q`
  - Passed: `17 passed`.

- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  - Passed.

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_001.py -q`
  - Passed: `32 passed`.

## Gaps

None. Broad repository validation, coverage gates, and CI-equivalent checks were
not run during the agent phase because AWF/GitHub owns that validation.
