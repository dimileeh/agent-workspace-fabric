# PRRT_kwDOSJAM6s6CNxRF Node Transient Codes Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6CNxRF` reports that dependency setup retries
miss Node package-manager registry failures when npm, pnpm, or yarn emit Node
error codes instead of phrase-shaped network errors. The fix is scoped to setup
dependency transient classification in `src/awf/runtime/validation.py` and
regression coverage in `tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Classify `EAI_AGAIN` from Node registry fetch output as a DNS transient.
- Classify `ETIMEDOUT` from Node registry fetch output as a timeout transient.
- Classify `ECONNRESET` from Node registry fetch output as a connection
  transient.
- Keep the retry bounded to dependency setup classification; do not broaden
  unrelated commands or deterministic failures.
- Preserve package/host evidence extraction where registry URLs are present.

## Implementation Steps

1. Add parameterized failing tests for npm, pnpm, and yarn setup failures that
   emit Node transient error codes.
2. Extend the setup transient regex categories with bounded Node error-code
   variants.
3. Run the focused validation test for setup dependency classification.
4. Save validation results in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- The new tests fail before the implementation change and pass after it.
