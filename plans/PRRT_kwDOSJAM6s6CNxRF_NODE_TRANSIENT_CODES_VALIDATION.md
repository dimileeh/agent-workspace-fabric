# PRRT_kwDOSJAM6s6CNxRF Node Transient Codes Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CNxRF_NODE_TRANSIENT_CODES_PLAN.md`

## Requirement Status

- Complete: Classify `EAI_AGAIN` from Node registry fetch output as a DNS
  transient.
  - Evidence: Added npm regression coverage in
    `tests/unit/runtime/test_validation.py`; extended the DNS setup transient
    pattern in `src/awf/runtime/validation.py`.
- Complete: Classify `ETIMEDOUT` from Node registry fetch output as a timeout
  transient.
  - Evidence: Added pnpm regression coverage; extended timeout setup transient
    patterns.
- Complete: Classify `ECONNRESET` from Node registry fetch output as a
  connection transient.
  - Evidence: Added yarn regression coverage; extended the connection setup
    transient pattern.
- Complete: Keep the retry bounded to dependency setup classification.
  - Evidence: The change is limited to `_SETUP_TRANSIENT_PATTERNS`; existing
    setup command gating remains unchanged.
- Complete: Preserve package/host evidence extraction where registry URLs are
  present.
  - Evidence: New tests assert registry hosts and package metadata; `.tgz`
    package artifact extraction was added for Node tarball URLs.

## Verification Evidence

- Before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k node_transient_error_codes`
  - Result: failed with all three new cases returning `classification is None`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k node_transient_error_codes`
  - Result: `3 passed, 193 deselected`.
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Result: `196 passed`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Result: `All checks passed!`

## Gaps

None.
