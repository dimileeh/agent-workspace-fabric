# PRRT_kwDOSJAM6s6F504S Companion Pre-build Timeout Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F504S_PREBUILD_TIMEOUT_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Pre-build capture timeout tracks `effective` and equals the inline `up()` cap. | Complete | `stack_launcher.launch()` now computes `effective_compose_up_timeout_seconds(...)` before pre-build and passes `_compose_up_capture_timeout_seconds(effective, wait=True)` (= `2*effective + 60`) into `_build_companion_services` → `CompanionImageBuilder.ensure` → `build_companion_image`. New launch-driven test asserts `1860.0` for effective `900`. |
| Pre-build never false-fails a build the inline path would have completed. | Complete | The forwarded value is byte-for-byte the same expression `ComposeManager.up(wait=True)` uses, so the pre-build cap can never be lower than the inline build cap. |
| Keep `COMPANION_BUILD_CAPTURE_TIMEOUT_SECONDS=1800.0` as the `build_companion_image` default-arg safety floor. | Complete | `build_companion_image` signature unchanged; only `ensure()` now forwards an explicit value. The four `test_build_companion_image_*` argv tests still pass unchanged. |
| Preserve the build-failure → inline `build:` fallback contract. | Complete | `ensure()` still returns `None` on failure/no-commit; `test_stack_launcher_companion_images.py` fallback tests pass. |
| Strict TDD; keep 99% coverage intact. | Complete | Failing-first regression captured (see below); new branch (effective hoist + forwarding) exercised by launch-driven and unit tests. Full coverage gate remains post-agent owned. |

## TDD Evidence

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_prebuilds_companion_with_effective_compose_budget -q`
  → `10 failed, 3 passed` (`TypeError: ensure() missing ... 'capture_timeout_seconds'`).
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_stack_launcher.py -q`
  → `46 passed`.
- Unaffected build argv tests:
  `pytest tests/unit/node/test_compose_manager.py -q -k "build_companion_image or companion_image"` → `6 passed, 40 deselected`.
- Focused lint/format/type:
  `ruff check` + `ruff format --check` on the 5 touched files → clean; `mypy src/awf/node/stack_launcher.py src/awf/node/companion_images.py` → `Success: no issues found in 2 source files`.

## Notes / residual behavior

- Worst-case pre-build wall time now equals the inline `up` cap (`2*effective + 60`,
  up to ~3660s when a companion sets `compose_up_timeout_seconds=1800`) instead of a
  flat 1800s. This is the intended direction — it only widens, never narrows, the
  success envelope relative to the inline build.
- At the default `startup_timeout_seconds=300` the pre-build cap is now `660s`
  (was an implicit 1800s); operators with genuinely slower cold builds raise the
  documented `compose_up_timeout_seconds` knob, which now lifts both the cached and
  inline allowances together (documented in `docs/CONCEPTS.md`).

Full AWF/GitHub validation (whole suite + 99% coverage gate) was not run in the
agent phase per the workspace contract; AWF owns broad validation and merge gating
after agent completion.
