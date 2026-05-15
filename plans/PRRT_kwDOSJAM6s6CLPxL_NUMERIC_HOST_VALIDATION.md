# PRRT_kwDOSJAM6s6CLPxL Numeric Host Validation

Plan reference: `PRRT_kwDOSJAM6s6CLPxL_NUMERIC_HOST_PLAN.md`

## Requirement Status

- Add a regression test showing a dependency setup network failure with only a
  version-like dotted token does not classify that token as a host: Complete.
  Added
  `test_setup_dependency_network_classifier_ignores_version_like_fallback_host`
  in `tests/unit/runtime/test_validation.py`.
- Preserve existing URL hostname extraction and fallback extraction for real
  hostnames: Complete. The URL-first extraction path is unchanged, and the
  fallback still returns non-file, non-numeric dotted host candidates.
- Keep the change local to setup dependency network classification: Complete.
  Changed only `src/awf/runtime/validation.py` plus the focused regression test
  and plan/validation docs.
- Run the narrow unit test coverage for the touched runtime validation behavior:
  Complete. The full runtime validation unit test file passed.
- Create validation documentation for this plan: Complete.

## Evidence

- Initial regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_version_like_fallback_host -q`
  failed because `classification.host` was `7.1.0`.
- Focused post-fix check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_version_like_fallback_host -q`
  passed.
- Runtime validation suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with `151 passed`.

## Remaining Gaps

None.
