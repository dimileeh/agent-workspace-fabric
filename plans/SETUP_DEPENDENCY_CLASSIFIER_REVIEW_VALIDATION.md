# Setup Dependency Classifier Review Validation

Plan reference: `SETUP_DEPENDENCY_CLASSIFIER_REVIEW_PLAN.md`

## Requirement Status

- Add regression coverage before implementation for the non-standard port case:
  Complete. Added parametrized coverage for `:401` and `:403` index URLs in
  `tests/unit/runtime/test_validation.py`.
- Add regression coverage before implementation for unrelated script output that
  contains `simple` plus a transient DNS phrase: Complete. Added coverage for
  `./build.sh` output containing plain `simple`.
- Preserve existing deterministic handling for recognizable HTTP 401/403 status
  contexts: Complete. Added explicit HTTP status context coverage for 401 and
  403 and kept 403 forbidden coverage green.
- Preserve existing transient retry classification for real dependency setup
  commands and PyPI `/simple/` output: Complete. Existing DNS/5xx coverage and
  a new `/simple/` fallback case pass.
- Keep changes scoped to validation classifier behavior and its unit tests:
  Complete. Implementation changes are limited to
  `src/awf/runtime/validation.py`; tests are limited to
  `tests/unit/runtime/test_validation.py`.

## Evidence

- Initial focused TDD run failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "plain_simple_context_fallback or index_port_as_http_auth_status or dependency_simple_index_fallback or http_auth_status_deterministic"`
  failed on the plain `simple` fallback and both port cases.
- Focused post-fix run passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "plain_simple_context_fallback or index_port_as_http_auth_status or dependency_simple_index_fallback or http_auth_status_deterministic"`
  passed with 6 tests.
- Full touched unit file passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 182 tests.
- Lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`.
- Typecheck passed:
  `uv run --python 3.12 --extra dev mypy src/awf`.

## Gaps

None.
