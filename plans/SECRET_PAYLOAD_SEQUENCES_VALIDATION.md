# Secret Payload Sequence Traversal Validation

Plan reference: `plans/SECRET_PAYLOAD_SEQUENCES_PLAN.md`

## Requirement Status

- Add a regression test proving sequence-contained secret-like values are rejected: Complete.
- Add a regression test proving mappings nested inside sequences are still scanned for secret-bearing keys: Complete.
- Update `_ensure_no_secret_payload` to recursively inspect list and tuple elements: Complete.
- Preserve sanitized error reporting without leaking secret values: Complete.
- Run focused host setup config tests only: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/SECRET_PAYLOAD_SEQUENCES_PLAN.md`
- `plans/SECRET_PAYLOAD_SEQUENCES_VALIDATION.md`

Focused checks:

- Pre-fix regression confirmation: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "secret_payloads_inside_lists or tuple_nested_secret_payloads"` failed with 3 failures because sequence-contained payloads were not classified as `HOST_SETUP_CONFIG_SECRET_VALUE`.
- Post-fix regression check: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "secret_payloads_inside_lists or tuple_nested_secret_payloads"` passed with 3 passed and 7 deselected.
- Focused unit file: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q` passed with 10 passed.
- Focused lint: `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py` passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation, provenance, logs, and merge gating after completion.
