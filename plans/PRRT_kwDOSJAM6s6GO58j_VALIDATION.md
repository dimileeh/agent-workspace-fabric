# PRRT_kwDOSJAM6s6GO58j Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GO58j_PLAN.md`

## Requirement Status

- Preserve the existing pre-launch companion image revalidation behavior:
  Complete. Existing revalidation tests still pass and the launch path still
  revalidates before the first `compose.up`.
- Detect a `docker compose up` failure that specifically reports a missing
  pre-built companion image tag rendered in the launch spec:
  Complete. `_missing_prebuilt_companion_image_retry_spec` matches
  missing-image output only when it names a pre-built companion image tag.
- Retry `docker compose up` once with the missing companion image cleared so
  Compose can build inline from the existing companion build context:
  Complete. The retry spec replaces only matching companion `image` values with
  `None`, preserving build context and Dockerfile fields.
- Do not retry unrelated Compose failures or profile service image failures:
  Complete. The focused negative regression confirms a missing non-companion
  image does not trigger retry.
- Add or update a regression test that fails without the post-revalidation retry:
  Complete. The new post-revalidation prune regression failed before the
  implementation and passes after it.
- Run focused tests for the touched behavior only:
  Complete. Focused checks were run; full AWF/GitHub validation remains managed
  by AWF after agent completion per workspace contract.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/PRRT_kwDOSJAM6s6GO58j_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GO58j_VALIDATION.md`

Focused checks:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  failed at
  `test_launch_retries_with_inline_build_when_prebuilt_image_pruned_after_revalidation`
  because the first `ComposeOperationError` escaped.
- Final test:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passed with `9 passed in 0.42s`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/node/stack_launcher.py`
  passed.

## Remaining Gaps

None for the scoped review-thread fix. Broad validation and merge gating are
left to AWF/GitHub after agent completion.
