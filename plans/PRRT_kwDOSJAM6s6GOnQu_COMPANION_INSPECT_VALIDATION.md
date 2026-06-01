# PRRT_kwDOSJAM6s6GOnQu Companion Inspect Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GOnQu_COMPANION_INSPECT_PLAN.md`

## Requirement Status

- Complete: Add a public `ComposeManager` method for strict companion image
  inspection.
  Evidence: `src/awf/node/compose_manager.py` adds
  `companion_image_inspect()`.

- Complete: Preserve existing lenient `ComposeManager.companion_image_exists()`
  behavior.
  Evidence: `companion_image_exists()` now delegates to the strict method but
  still catches any `ComposeOperationError`; regression coverage added in
  `tests/unit/node/test_compose_manager.py`.

- Complete: Preserve builder launch-time behavior.
  Evidence: `CompanionImageBuilder.companion_image_exists()` now calls the
  public strict method; focused builder tests cover present, missing,
  missing-misclassified-as-unavailable, and non-missing probe failures.

- Complete: Remove direct private `_docker_capture()` access and `SLF001`
  suppression from the builder.
  Evidence: `src/awf/node/companion_images.py` no longer calls
  `_docker_capture()` or uses `# noqa: SLF001`.

- Complete: Validate with focused tests and lint only.
  Evidence: see focused command results below. Full AWF/GitHub validation is
  intentionally left to AWF after agent completion per workspace contract.

## TDD Evidence

Initial focused test run after updating tests first failed as expected:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestCompanionImageCommands tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_true_when_tag_present tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_false_when_tag_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_probe_error_reason_code tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_unexpected_inspect_failure -q
```

Result: `8 failed, 7 passed`, with failures showing missing
`ComposeManager.companion_image_inspect()` and builder access to private
`_docker_capture()`.

## Final Verification

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestCompanionImageCommands tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_true_when_tag_present tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_false_when_tag_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_probe_error_reason_code tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_unexpected_inspect_failure -q
```

Result: `15 passed in 0.56s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py src/awf/node/companion_images.py tests/unit/node/test_compose_manager.py tests/unit/node/test_companion_images.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/node/compose_manager.py src/awf/node/companion_images.py
```

Result: `Success: no issues found in 2 source files`.

## Gaps

None.
