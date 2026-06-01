# PRRT_kwDOSJAM6s6GOlX8 Companion Revalidation Concurrency Validation

Plan reference:
`PRRT_kwDOSJAM6s6GOlX8_COMPANION_REVALIDATION_CONCURRENCY_PLAN.md`

## Requirement Status

- Revalidate companion images concurrently when a builder is configured:
  Complete. `_revalidate_prebuilt_companion_images` now dispatches per-companion
  image probes with `asyncio.gather`.
- Preserve existing behavior for companions without an image, existing images,
  missing images, and unchanged specs:
  Complete. The helper still skips image-less companions, keeps existing images,
  clears missing images, and returns the original spec when the revalidated tuple
  is unchanged.
- Add a regression test that fails if image-existence probes are dispatched
  sequentially:
  Complete. `test_companion_image_revalidation_runs_concurrently` uses an async
  barrier that times out under the original sequential loop.
- Run only focused validation owned by this change:
  Complete. Focused tests and lint were run locally. Full AWF/GitHub validation
  is intentionally left to the AWF post-agent and CI merge gates.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/PRRT_kwDOSJAM6s6GOlX8_COMPANION_REVALIDATION_CONCURRENCY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GOlX8_COMPANION_REVALIDATION_CONCURRENCY_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_companion_image_revalidation_runs_concurrently -q`
  failed before implementation with `TimeoutError`, proving the regression test
  catches sequential revalidation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_companion_image_revalidation_runs_concurrently -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passed with `7 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/stack_launcher.py`
  passed.

## Gaps

No planned gaps remain.
