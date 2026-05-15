# PRRT_kwDOSJAM6s6CMUUb Host Redaction Validation

Plan reference: `PRRT_kwDOSJAM6s6CMUUb_HOST_REDACTION_PLAN.md`

## Requirement Status

- Add a regression test that fails when a JWT-like dotted secret is extracted as
  setup dependency `host`: Complete. Added
  `test_setup_dependency_network_classifier_does_not_extract_jwt_secret_as_host`.
  Confirmed it failed before implementation because the JWT-like value became
  `classification.host`.
- Preserve valid URL host extraction, including URLs with credentials in the
  userinfo section: Complete. Existing tests in `tests/unit/runtime/test_validation.py`
  still pass, including credentialed index URL coverage.
- Preserve existing fallback-host behavior for safe, non-secret dotted
  hostnames: Complete. Existing fallback-host classifier tests in the module
  still pass.
- Reject host candidates that would be changed by `redact_audit_text`:
  Complete. `_extract_setup_dependency_host` now skips URL-derived and fallback
  candidates unless `_is_safe_setup_dependency_host` accepts them.
- Run targeted validation for the changed runtime validation behavior:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_does_not_extract_jwt_secret_as_host -q
```

Initial result before implementation: failed because the JWT-like secret was
returned as `classification.host`.

After implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Result: 167 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Result: all checks passed.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Result: both files already formatted.

## Gaps

None.
