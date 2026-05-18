# Comment 4482045018 Init Env Review Plan

## Problem Statement

Address the review-level feedback on PR #264 about `awf init` env seeding. The
comment identifies private bootstrap API coupling, ambiguous seeding failure
warnings, and a fragile write-failure test helper.

## Scope

- Keep `awf init` env routing behavior unchanged.
- Replace the CLI's cross-module private bootstrap asset-root call with a public
  service helper.
- Make env seeding warnings distinguish parent-directory creation failures from
  file write failures.
- Preserve the already-normalized write-failure test helper behavior.

## Requirements Checklist

- [ ] Add or use an explicit public bootstrap asset-root helper.
- [ ] Update CLI env-path resolution to call the public helper.
- [ ] Update tests so asset-root stubbing targets the public helper.
- [ ] Add regression coverage for parent-directory creation failure messaging.
- [ ] Keep machine-readable `env_action == "write_failed"` for seeding failures.
- [ ] Confirm the path-write failure helper compares normalized paths.

## Implementation Steps

1. Update `tests/unit/cli/test_init.py` first to stub the public helper and add a
   mkdir-failure regression test.
2. Run the targeted test subset and confirm it fails before implementation when
   practical.
3. Add a public `get_bootstrap_asset_root()` helper in `awf.service.bootstrap`.
4. Update `src/awf/cli/main.py` to call the public helper and split env-seeding
   directory/write error handling.
5. Re-run targeted unit tests and relevant quality checks.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/cli/test_init.py`

Pass criteria: targeted tests pass, lint passes, and the validation document
records every requirement as complete or explicitly explains any gap.
