# Issue #335 Plan: Companion Image Launch Revalidation

## Goal

Close the companion-image pre-build vs. GC-prune race in `StackLauncher` without changing GC behavior. If a pre-built companion image disappears after `ensure()` but before `docker compose up`, revalidate at launch point and fall back to inline `build:` for that companion.

## Implementation

1. Add regression tests first:
   - cache-hit image still present: launch keeps the companion `image`;
   - cache-hit image vanished: launch clears the image so compose renders/builds inline;
   - image existence helper returns true/false and preserves probe errors.
2. Add a launch-time image existence helper on `CompanionImageBuilder`.
3. Revalidate pre-built companion images after `WorkspaceComposeSpec` construction and immediately before `compose.up`.
4. Replace only confirmed-missing companion images with `image=None`; leave still-present images unchanged.
5. Do not modify GC and do not add permanent `build:` alongside normal pre-built `image:` rendering.

## Focused Validation

Run targeted checks for the changed code and regression coverage only:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_compose_manager.py::TestRender::test_companion_prebuilt_image_pins_pull_policy_never -q
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_images.py src/awf/node/stack_launcher.py tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py
uv run --python 3.12 --extra dev mypy src/awf/node/companion_images.py src/awf/node/stack_launcher.py
```

Full AWF/GitHub validation remains owned by AWF after agent completion.
