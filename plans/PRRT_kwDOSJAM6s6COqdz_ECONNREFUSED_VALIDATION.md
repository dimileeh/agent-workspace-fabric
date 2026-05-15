# PRRT_kwDOSJAM6s6COqdz ECONNREFUSED Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6COqdz_ECONNREFUSED_PLAN.md`

## Requirement Status

- Complete: Classify Node `ECONNREFUSED` registry fetch output as a
  `connection` transient.
  - Evidence: Added an npm `connect ECONNREFUSED registry.npmjs.org:443`
    regression case in `tests/unit/runtime/test_validation.py` and extended the
    connection transient pattern in `src/awf/runtime/validation.py`.
- Complete: Preserve package and host extraction from the registry tarball URL.
  - Evidence: The new regression asserts `react==18.2.0` and
    `registry.npmjs.org`.
- Complete: Keep the retry bounded to setup dependency classification.
  - Evidence: The implementation only changes `_SETUP_TRANSIENT_PATTERNS`; the
    setup command and dependency-evidence gates are unchanged.

## Verification Evidence

- Before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k node_transient_error_codes`
  - Result: failed with the new `ECONNREFUSED` case returning
    `classification is None`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k node_transient_error_codes`
  - Result: `4 passed, 217 deselected`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Result: `All checks passed!`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Result: `221 passed`.

## Gaps

None.
