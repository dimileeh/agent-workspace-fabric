# PRRT_kwDOSJAM6s6Ffq3m Source Marker OSError Plan

## Problem Statement and Scope

The review thread reports that `validate_source_checkout()` records a marker as both unreadable and missing when `Path.is_file()` or `Path.is_dir()` raises `OSError`. The fix is limited to source-checkout marker classification in `src/awf/host_setup/source_assets.py` and a focused regression test.

## Requirements Checklist

- Keep actual missing markers in `SourceCheckoutError.missing_markers`.
- Keep marker stat/probe `OSError` failures in `details["unreadable_paths"]`.
- Do not report an unreadable marker as missing after an `OSError`.
- Preserve existing source checkout validation behavior outside this classification path.

## Implementation Steps

1. Add a focused unit test that forces a marker `is_file()` probe to raise `OSError`.
2. Update marker validation to continue after recording the unreadable marker.
3. Run the targeted unit test file or selected test nodes that cover this behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Passes with the new regression and existing source checkout config tests.

Full AWF/GitHub validation is intentionally left to AWF after agent completion per the workspace contract.
