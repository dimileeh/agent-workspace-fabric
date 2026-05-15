# Review Comment 4454303511 Dependency Retry Validation

Plan reference: `plans/comment_4454303511_dependency_retry_review_PLAN.md`

## Requirement Status

- Complete: Added
  `test_setup_dependency_network_diagnostic_normalization_uses_bounded_input`
  to prove diagnostic whitespace normalization receives at most
  `4 * _SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT` characters.
- Complete: `_setup_dependency_network_diagnostic` now slices combined output
  to `_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT` before regex
  normalization, preserving existing audit redaction and durable length limits.
- Complete: Added
  `test_setup_dependency_network_classifier_skips_unknown_wrapper_after_successful_package_output`
  to cover an unknown wrapper script with successful package install evidence
  followed by an unrelated DNS failure.
- Complete: `_looks_like_dependency_setup` now requires unknown wrapper output
  to contain dependency/package-index evidence co-located with the transient
  failure, matching the tighter compound-command fallback.
- Complete: Focused and broader validation commands passed.

## Evidence

Changed files:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/comment_4454303511_dependency_retry_review_PLAN.md`
- `plans/comment_4454303511_dependency_retry_review_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_diagnostic_normalization_uses_bounded_input tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_unknown_wrapper_after_successful_package_output -q
```

Result before implementation: failed both new regression tests.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_diagnostic_normalization_uses_bounded_input tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_unknown_wrapper_after_successful_package_output -q
```

Result after implementation: passed, `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "setup_dependency_network_classifier or setup_dependency_diagnostics or setup_dependency_network_diagnostic"
```

Result: passed, `59 passed, 142 deselected`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Result: passed, `201 passed`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: passed, no issues found.
