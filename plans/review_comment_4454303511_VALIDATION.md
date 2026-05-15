# Review Comment 4454303511 Validation

Plan reference: `plans/review_comment_4454303511_PLAN.md`

## Requirement Status

- Verify current recovery-path coverage for monitor-dispatched setup dependency
  retry exhaustion: Complete.
  `tests/unit/control/test_executor_monitor_recovery.py` already includes
  `test_setup_dependency_exhaustion_during_recovery_preserves_monitor_reason`,
  which covers monitor recovery after setup dependency retry exhaustion.
- Add or update a failing regression test for any real gap before changing
  implementation: Complete.
  Added
  `test_setup_dependency_network_classifier_ignores_bare_5xx_phrase_without_http_context`;
  it failed before the classifier change because bare `service unavailable`
  matched `http_5xx`.
- Tighten `http_5xx` classification so bare numeric 5xx text is only treated as
  transient when it is clearly HTTP/status-code context, while preserving known
  dependency fetch 5xx behavior: Complete.
  The classifier now requires phrase-only 5xx signals such as `service
  unavailable` to have nearby HTTP, status-code, response, or package-index
  context. A positive HTTP-context regression test preserves dependency-index
  5xx retry behavior.
- Keep setup retry metadata, event emission, and failure reason behavior
  unchanged except for the narrowed false-positive classification: Complete.
  The change is limited to the runtime transient pattern and classifier tests.
- Commit only the files changed for this comment using a conventional commit
  message: Complete after local commit.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/review_comment_4454303511_PLAN.md`
- `plans/review_comment_4454303511_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_bare_5xx_phrase_without_http_context -q`
  failed before implementation, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_ignores_bare_5xx_phrase_without_http_context tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_retries_5xx_phrase_with_http_context -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed: 192 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q`
  passed: 42 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_monitor_recovery.py`
  passed.
