# Init Env Error JSON Review 4482045018 Plan

## Problem Statement and Scope

Greptile's review-level comment found that `_init_env_error_payload()` stores
raw path strings in the JSON `env_error` payload. When `awf init` runs from a
source-checkout subdirectory and `_resolve_init_env_paths()` returns asset-root
absolute paths, JSON clients see absolute paths while pretty-mode warnings show
launch-directory-relative paths.

This iteration is limited to normalizing env seeding failure payload paths in
`src/awf/cli/main.py` and adding focused regression coverage in
`tests/unit/cli/test_init.py`.

## Requirements Checklist

- Preserve existing env seeding behavior and error operations.
- Normalize `env_error.path`, `env_error.env_file`, and
  `env_error.env_example` through the existing init display-path logic.
- Cover the non-CWD asset-root JSON write-failure scenario that previously
  returned absolute paths.
- Keep secret values out of init output and payload assertions.
- Validate with a failing-first focused test, the full init CLI test file, and
  ruff for the touched files.

## Implementation Steps

1. Add a focused JSON regression for an asset-root compose env write failure
   from a project subdirectory.
2. Run the focused test before implementation and confirm it fails with raw
   absolute paths.
3. Update `_init_env_error_payload()` to use `_init_display_path()` for all
   payload path fields.
4. Re-run the focused test, the full `tests/unit/cli/test_init.py` file, and
   ruff.
5. Record requirement status and command evidence in
   `plans/INIT_ENV_ERROR_JSON_4482045018_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_json_normalizes_asset_root_env_write_failure -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`

Pass criteria: the focused test fails before the implementation change and
passes after; the full init CLI unit file and ruff pass; JSON payload paths are
relative to the launch directory where possible.
