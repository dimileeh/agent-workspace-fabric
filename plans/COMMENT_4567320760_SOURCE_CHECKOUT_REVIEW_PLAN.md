# Comment 4567320760 Source Checkout Review Plan

## Problem Statement and Scope

Address review comment `issue:4567320760` on PR #295. The comment identifies two maintainability issues in the new host setup source-checkout code:

- the test helper hardcodes `"docs"` instead of deriving directory marker behavior from `SOURCE_CHECKOUT_MARKERS`;
- `SourceCheckoutError.to_dict()` can serialize missing markers both at the top level and inside `details`.

Scope is limited to the host setup source-checkout implementation, the focused unit tests, and this plan/validation record.

## Requirements Checklist

- [x] Update `_write_valid_source_checkout` to derive marker creation behavior from marker kind metadata.
- [x] Keep missing marker diagnostics available through `SourceCheckoutError.missing_markers` and top-level `to_dict()["missing_markers"]`.
- [x] Remove duplicate `details["missing_markers"]` serialization from invalid source-checkout details.
- [x] Add or update regression assertions proving missing marker details are not duplicated.
- [x] Run focused host setup unit tests only; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Update the test helper to iterate over `SOURCE_CHECKOUT_MARKERS` and create files/directories according to `marker.kind`.
2. Change `_source_checkout_details` so it only includes unreadable path details.
3. Update missing-marker tests to assert `details` does not duplicate `missing_markers`.
4. Run the focused unit test file for host setup.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`

Pass criteria: the focused test file passes and no broad validation suite is executed in the agent phase.

## Assumptions/Changes

- Added a focused ruff check for the two touched Python files to catch lint regressions without running broad validation.
