# Address PRRT_kwDOSJAM6s6GOmMW Plan

## Problem Statement and Scope

Launch-time companion image revalidation should clear a pruned prebuilt image so
the stack can fall back to inline `build:`. The review thread reports that
Docker's missing-image inspect stderr includes "daemon", causing
`_docker_capture` to classify the error as `DOCKER_UNAVAILABLE`; the current
missing-image helper only accepts `COMPOSE_COMMAND_FAILED`, so a confirmed
missing image can abort launch as a probe error.

Scope is limited to companion-image revalidation behavior and its focused unit
tests.

## Requirements Checklist

- Add a regression test for a missing-image inspect error classified as
  `DOCKER_UNAVAILABLE`.
- Preserve propagation of genuine Docker probe failures.
- Keep confirmed missing-image inspect failures returning `False` so callers can
  clear the companion image and fall back to inline build.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add the focused regression test in `tests/unit/node/test_companion_images.py`.
2. Confirm the regression fails with the current implementation.
3. Update `_is_missing_image_inspect_failure` in
   `src/awf/node/companion_images.py` to recognize confirmed missing-image
   stderr even when Docker classified it as unavailable.
4. Run the focused companion-image tests and a targeted lint check for the
   touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_images.py tests/unit/node/test_companion_images.py`
  passes.
- Full AWF/GitHub validation remains owned by AWF after agent completion.
