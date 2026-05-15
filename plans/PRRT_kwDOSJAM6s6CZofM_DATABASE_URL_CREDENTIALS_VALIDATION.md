# PRRT_kwDOSJAM6s6CZofM Database URL Credentials Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CZofM_DATABASE_URL_CREDENTIALS_PLAN.md`

## Requirement Status

- Complete: Added a regression proving production guardrails do not raise a raw
  port-parsing `ValueError` for non-default database credentials with a
  malformed port.
- Complete: Preserved structured rejection of bundled AWF local database
  credentials in production, including when the URL port is malformed.
- Complete: Avoided broad URL validation and unrelated config refactors.
- Complete: Ran the narrow focused tests proving the review-thread behavior.
- Complete: Prepared local changes for a thread-specific commit.

## Evidence

Files changed:

- `src/awf/common/config.py`
- `tests/unit/service/test_config.py`
- `plans/PRRT_kwDOSJAM6s6CZofM_DATABASE_URL_CREDENTIALS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CZofM_DATABASE_URL_CREDENTIALS_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q -k "malformed_port"`
  failed with two `ValueError: Port could not be cast...` failures.
- Targeted behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q -k "malformed_port"`
  passed with `2 passed, 54 deselected`.
- Focused config suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  passed with `56 passed`.
- Lint check:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py`
  passed.

## Gaps

None.
