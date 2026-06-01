# Issue #335 Validation: Companion Image Launch Revalidation

## Result

Implemented the point-of-use defense for issue #335. `ComposeStackLauncher` now revalidates pre-built companion images after rendering `WorkspaceComposeSpec` and immediately before `compose.up`. A confirmed-missing image is cleared from that companion service so the existing compose renderer emits inline `build:` for the launch; still-present images remain unchanged as `image:` with the existing `pull_policy: never` rendering.

## Requirement Check

- Revalidate immediately before launch: satisfied in `ComposeStackLauncher._revalidate_prebuilt_companion_images`, called directly before `self._compose.up(spec, wait=True)`.
- Fall back to inline build only for vanished cached images: satisfied by replacing only confirmed-missing companion services with `image=None`.
- Keep normal cache-hit rendering unchanged: satisfied; still-present tags remain on the companion service and `test_companion_prebuilt_image_pins_pull_policy_never` passes.
- Preserve probe errors distinctly from missing images: satisfied; `CompanionImageBuilder.companion_image_exists` returns `False` only for missing-image inspect diagnostics and re-raises Docker/probe failures with their original `ComposeOperationError.reason_code`.
- Do not modify GC: satisfied.

## Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_compose_manager.py::TestRender::test_companion_prebuilt_image_pins_pull_policy_never -q
# 28 passed

uv run --python 3.12 --extra dev ruff check src/awf/node/companion_images.py src/awf/node/stack_launcher.py tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py
# All checks passed

uv run --python 3.12 --extra dev ruff format --check src/awf/node/companion_images.py src/awf/node/stack_launcher.py tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py
# 4 files already formatted

uv run --python 3.12 --extra dev mypy src/awf/node/companion_images.py src/awf/node/stack_launcher.py
# Success: no issues found in 2 source files
```

Full AWF/GitHub validation, full-repository tests, and coverage gates are intentionally left to AWF after agent completion per the workspace contract.
