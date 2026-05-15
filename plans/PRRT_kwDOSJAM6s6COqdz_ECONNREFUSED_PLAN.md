# PRRT_kwDOSJAM6s6COqdz ECONNREFUSED Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6COqdz` reports that setup dependency retry
classification misses Node package-manager registry failures emitted as
`connect ECONNREFUSED registry.npmjs.org:443`. The fix is scoped to setup
dependency transient classification in `src/awf/runtime/validation.py` and
regression coverage in `tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Classify Node `ECONNREFUSED` registry fetch output as a `connection`
  transient.
- Preserve package and host extraction from the registry tarball URL.
- Keep the retry bounded to setup dependency classification; do not broaden
  non-dependency setup failures or deterministic failures.

## Implementation Steps

1. Add a failing regression case for `connect ECONNREFUSED registry.npmjs.org:443`
   to the existing Node transient-code classifier test.
2. Extend the setup connection transient pattern to include the Node
   `ECONNREFUSED` error code.
3. Run the focused regression test before and after the implementation change.
4. Save validation results in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k node_transient_error_codes`
  fails before the implementation change with the new `ECONNREFUSED` case.
- The same focused command passes after the implementation change.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
