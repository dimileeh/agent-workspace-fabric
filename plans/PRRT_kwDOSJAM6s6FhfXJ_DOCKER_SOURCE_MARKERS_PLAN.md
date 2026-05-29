# PRRT_kwDOSJAM6s6FhfXJ Docker Source Markers Plan

## Problem Statement and Scope

The PR review thread reports that `validate_source_checkout()` can accept an AWF
source checkout that has the current source markers but is missing files or
directories copied by `docker/control-plane.Dockerfile`. The later local-service
control-plane Docker build then fails even though source validation should have
raised `SOURCE_CHECKOUT_INVALID`.

Scope is limited to the host setup source-checkout marker contract, focused unit
coverage, and this plan/validation record.

## Requirements Checklist

- [ ] Missing Docker build/package bootstrap inputs copied by
  `docker/control-plane.Dockerfile` fail source validation with
  `SOURCE_CHECKOUT_INVALID`.
- [ ] Missing Docker build/package bootstrap inputs are reported through
  `SourceCheckoutError.missing_markers` using root-relative marker paths.
- [ ] Existing valid checkout metadata records the expanded marker contract so
  stale metadata detection can detect the new contract.
- [ ] Validation remains focused; full AWF/GitHub validation is left to the AWF
  post-agent pipeline.

## Implementation Steps

1. Add a focused failing unit test that removes each Docker build/package
   bootstrap input from a generated valid source checkout and expects
   `SOURCE_CHECKOUT_INVALID`.
2. Update `SOURCE_CHECKOUT_MARKERS` in
   `src/awf/host_setup/source_assets.py` to include the Docker build/package
   bootstrap inputs copied by `docker/control-plane.Dockerfile`.
3. Run the targeted unit test before and after the implementation, plus a
   focused lint check on the touched files.
4. Write `plans/PRRT_kwDOSJAM6s6FhfXJ_DOCKER_SOURCE_MARKERS_VALIDATION.md`
   with requirement status and focused evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  passes.
- Full AWF/GitHub validation is not run in the agent phase per workspace
  contract.
