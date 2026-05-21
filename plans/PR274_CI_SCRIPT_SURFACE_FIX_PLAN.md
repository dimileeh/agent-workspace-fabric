# PR274 CI Script Surface Fix Plan

## Problem Statement and Scope

PR #274 fails the `python-full-coverage` job because
`tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators`
finds `scripts/ci_docker_build_retry.py` in the repository-root `scripts/` directory.

The root `scripts/` directory is intentionally limited to supported public generator scripts. The
Docker build retry helper is CI-only implementation detail, so the fix should preserve the helper
and its regression coverage while moving it out of the public script surface.

Scope is limited to the CI retry helper location, its focused unit test import path, and plan
validation for this repair.

## Requirements Checklist

- Keep all work on the current AWF-managed branch; do not push or switch branches.
- Do not disable, skip, weaken, or broaden the failing docs cleanup check.
- Remove the CI-only retry helper from the root `scripts/` directory.
- Preserve the retry helper implementation and focused regression tests.
- Run the provided focused repro before and after the fix.
- Run narrow lint/tests for the moved helper and affected tests.
- Commit the fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Move `scripts/ci_docker_build_retry.py` to a GitHub Actions-owned helper path outside the root
   `scripts/` directory.
2. Update `tests/unit/test_ci_docker_build_retry.py` to import the helper from its new path without
   adding the hidden CI directory to the public Python package surface.
3. Re-run the focused docs cleanup repro and the retry helper unit tests.
4. Run narrow Ruff checks for the moved helper and affected tests.
5. Record validation evidence in `plans/PR274_CI_SCRIPT_SURFACE_FIX_VALIDATION.md`.
6. Commit all local changes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q`
  - Passes and proves the root `scripts/` directory surface is clean.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_docker_build_retry.py -q`
  - Passes and proves retry behavior is preserved.
- `uv run --python 3.12 --extra dev ruff check .github/scripts/ci_docker_build_retry.py tests/unit/test_ci_docker_build_retry.py tests/unit/docs/test_api_surface_cleanup_docs.py`
  - Passes for changed Python/test surfaces.
- `uv run --python 3.12 --extra dev ruff format --check .github/scripts/ci_docker_build_retry.py tests/unit/test_ci_docker_build_retry.py tests/unit/docs/test_api_surface_cleanup_docs.py`
  - Passes for formatting.
