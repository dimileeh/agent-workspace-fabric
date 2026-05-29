# Comment 3323207473 Redact Map Keys Validation

Plan reference: `COMMENT_3323207473_REDACT_MAP_KEYS_PLAN.md`

## Requirement Status

- Add a regression test proving token-looking and provider-reference mapping keys are redacted in both JSON and pretty first-run output: Complete.
- Preserve existing provider-ref key-name behavior where keys such as `credential_ref` and `provider_ref` redact their values: Complete.
- Keep tuple-preservation behavior for first-run redaction: Complete.
- Run only focused tests for the touched rendering behavior; broad AWF/GitHub validation remains owned by AWF after agent completion: Complete.

## Evidence

- Changed `src/awf/host_setup/rendering.py` to redact first-run mapping keys before inserting them into returned mappings.
- Added `tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_token_and_provider_ref_detail_keys`.
- Confirmed the new regression failed before the implementation with `KeyError: '[redacted]'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_token_and_provider_ref_detail_keys -q` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q` passed: 18 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/rendering.py` passed.

No gaps remain. Full AWF/GitHub validation was not run locally; AWF owns broad validation after agent completion.
