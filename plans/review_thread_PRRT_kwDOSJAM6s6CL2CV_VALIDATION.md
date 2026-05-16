# Review Thread PRRT_kwDOSJAM6s6CL2CV Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CL2CV_PLAN.md`

## Requirement status

- Add a regression test that fails when a credential-bearing index URL is
  captured as the package: Complete. Added
  `test_setup_dependency_network_classifier_skips_index_url_credentials_for_package`.
- Ensure setup dependency package extraction does not emit URL credentials or
  known tokens: Complete. Package extraction now searches URL-userinfo-stripped
  text and skips candidates that audit redaction would modify.
- Preserve package extraction for the real dependency in the setup output or
  non-secret command text: Complete. The regression asserts the classifier
  reports `docker==7.1.0` from stderr instead of the index URL credential.
- Keep host extraction and diagnostic redaction behavior unchanged: Complete.
  Host extraction still returns `files.pythonhosted.org`; diagnostic handling
  was not changed.
- Run the narrow affected tests and relevant lint for touched files: Complete.

## Evidence

- Changed `src/awf/runtime/validation.py`.
- Changed `tests/unit/runtime/test_validation.py`.
- Added plan and validation records under `plans/`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_index_url_credentials_for_package -q`
  failed with `classification.package` equal to
  `ghp_1234567890abcdef@files.pythonhosted.org`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 154 tests.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.

## Gaps

None.
