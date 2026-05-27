# PRRT_kwDOSJAM6s6FPNFr Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FPNFr_PLAN.md`

## Requirement Status

- Complete: Preserve absolute host resolution for companion `build_context`.
  - Evidence: `companion_service_from_materialized` still resolves `spec.build_context` through
    `_resolve_repo_path`; regression assertions cover the absolute rendered context.
- Complete: Keep the default companion `Dockerfile` relative to the rendered build context.
  - Evidence: `test_companion_service_from_materialized_keeps_default_dockerfile_context_relative`
    fails on the old absolute-root behavior and passes after the fix.
- Complete: Preserve support for explicit repo-relative companion Dockerfile paths without allowing
  checkout escapes.
  - Evidence: `_resolve_companion_dockerfile` validates non-default Dockerfile paths against the
    companion checkout root, then renders them relative to the resolved build context.
- Complete: Keep existing companion path safety behavior for `build_context`, `env_file`, and
  repo-relative volume sources.
  - Evidence: existing focused companion-services tests pass unchanged.
- Complete: Do not run broad AWF/GitHub-owned validation.
  - Evidence: only targeted unit and lint commands listed below were run; full validation remains
    managed by AWF/GitHub after agent completion.

## Commands Run

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q -k 'default_dockerfile_context_relative or explicit_repo_relative_dockerfile'`
  - Result before implementation: failed with old absolute Dockerfile rendering.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  - Result: passed, 21 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q -k companion`
  - Result: passed, 4 selected tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py`
  - Result: passed.

## Gaps

None for this review-thread scope.
