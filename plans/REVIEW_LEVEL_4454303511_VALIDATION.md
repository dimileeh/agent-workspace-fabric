# Review Level 4454303511 Validation

Plan reference: `plans/REVIEW_LEVEL_4454303511_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for artifact-like fallback host candidates
  including `.tar.gz`, `.whl`, `.cfg`, `.yml`, `.yaml`, and `.json`.
- Complete: Added regression coverage that an HTTP 503 dependency failure with a
  "temporarily forbidden" body remains retryable.
- Complete: Added regression coverage that an explicit HTTP 403 Forbidden
  failure remains deterministic and is not classified as retryable.
- Complete: Updated matching narrowly in `src/awf/runtime/validation.py` by
  expanding fallback artifact suffix exclusions and replacing the broad
  standalone `forbidden` deterministic match with an explicit 403/Forbidden
  context.
- Complete: Verified the changed classifier behavior with focused and broader
  unit checks.

## Evidence

- Changed `src/awf/runtime/validation.py`.
- Changed `tests/unit/runtime/test_validation.py`.
- Confirmed new regression tests failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "artifact_like_fallback_hosts or retries_503_temporarily_forbidden_body or keeps_403_forbidden_deterministic"`
  failed with 7 failing assertions and 1 passing guardrail assertion.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "artifact_like_fallback_hosts or retries_503_temporarily_forbidden_body or keeps_403_forbidden_deterministic"`
  passed with 8 tests.
- Passing classifier slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "setup_dependency_network_classifier"`
  passed with 34 tests.
- Passing touched module:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 175 tests.
- Passing lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`.

## Gaps

No remaining gaps.
