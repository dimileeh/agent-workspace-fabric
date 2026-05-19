# Address Review Comment 4482045018 Plan

## Problem Statement and Scope

Greptile's review-level PR comment raised three `awf init` env seeding concerns:
first-run Docker preflight visibility, inconsistent pretty-output streams for
skipped seeding notices, and absolute-vs-relative path feedback when invoked
from a source checkout subdirectory.

The current branch already seeds the env file before loading service settings
and includes regression coverage for seeded `AWF_DOCKER_HOST` preflight values.
This iteration is limited to the two remaining operator-output issues in
`src/awf/cli/main.py`, with focused regressions in `tests/unit/cli/test_init.py`.

## Requirements Checklist

- Preserve the existing seeded-preflight behavior and regression coverage.
- Emit pretty-mode `write_failed` and `no_example` env seeding notices through a
  consistent stream.
- Normalize pretty-mode env seeding paths relative to the launch directory when
  possible, including source-checkout subdirectory launches.
- Keep JSON payload paths and existing no-secret guarantees intact.
- Validate with failing-first focused tests, the full init unit file, and ruff.

## Implementation Steps

1. Add or update focused tests showing write-failure and missing-example notices
   are both emitted on stdout, not split across stdout/stderr.
2. Add or update focused tests showing asset-root compose env messages from a
   project subdirectory use launch-directory-relative paths instead of absolute
   paths.
3. Implement a small pretty-path formatter and use it only for human-readable
   env seeding messages.
4. Switch the write-failure pretty notice to the same stream as no-example
   notices.
5. Run focused tests, then all `tests/unit/cli/test_init.py`, then ruff.
6. Update `plans/ADDRESS_REVIEW_4482045018_VALIDATION.md` with requirement
   status and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_warns_when_env_write_fails tests/unit/cli/test_init.py::test_init_without_path_warns_when_compose_env_examples_missing tests/unit/cli/test_init.py::test_init_without_path_prefers_asset_root_compose_env_from_subdirectory tests/unit/cli/test_init.py::test_init_without_path_prefers_asset_root_compose_example_from_subdirectory tests/unit/cli/test_init.py::test_init_without_path_does_not_seed_non_root_compose_dir -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`

Pass criteria: focused tests fail before implementation and pass after; full
init tests and ruff pass with no secret values added to output assertions.
