# Review 4482045018 Trailing Context Plan

## Problem Statement And Scope

Address the latest review-level feedback for PR comment `issue:4482045018`.
The work is limited to two `awf init` env-seeding concerns:

- Preserve blank/comment context that trails the final root `.env` overlay-only
  assignment when merging into `docker/compose/.env`.
- Make bootstrap-mode init tests independent from the real `Settings`
  constructor when `_stub_bootstrap_mode()` already stubs service settings.

## Requirements Checklist

- [ ] Add a failing regression test for trailing overlay-only `.env` context.
- [ ] Update `_merge_env_seed_contents()` to preserve that trailing context
  without appending unrelated root-only comments when no overlay-only assignment
  was merged.
- [ ] Add a failing regression test proving `_stub_bootstrap_mode()` replaces
  the real `Settings` constructor.
- [ ] Update `_stub_bootstrap_mode()` to patch `awf.common.config.Settings` with
  a minimal test double for helper-backed bootstrap-mode tests.
- [ ] Run focused tests for the changed behavior and record validation evidence.
- [ ] Commit only the files changed for this review fix on the current AWF
  branch.

## Implementation Steps

1. Add focused tests in `tests/unit/cli/test_init.py` for the trailing context
   merge and explicit `Settings` test double behavior.
2. Run those tests before implementation and confirm they fail.
3. Preserve pending overlay context at the end of
   `_merge_env_seed_contents()` when at least one overlay-only assignment is
   being appended.
4. Patch `awf.common.config.Settings` inside `_stub_bootstrap_mode()` with a
   minimal constructor that records kwargs for assertions.
5. Re-run focused tests, then run the full init unit test file and ruff on the
   touched files.
6. Create `plans/REVIEW_4482045018_TRAILING_CONTEXT_VALIDATION.md` with
   requirement status and command evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_preserves_trailing_root_env_overlay_context tests/unit/cli/test_init.py::test_stub_bootstrap_mode_replaces_settings_constructor -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py
```

Pass criteria: the focused regressions fail before implementation, then all
listed commands pass after implementation.
