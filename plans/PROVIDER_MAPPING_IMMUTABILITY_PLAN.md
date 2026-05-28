# Provider Mapping Immutability Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6Ff_7H` reports that `HostSetupConfig.providers`
is a mutable `dict`. `HostSetupConfig` is frozen, but callers can still mutate
the dict in place after validation and place raw provider payloads on a config
object that is intended to be non-secret-bearing.

Scope is limited to host setup config model immutability and focused regression
coverage for the provider state boundary. Broad AWF/GitHub validation remains
owned by AWF after this agent cycle.

## Requirements Checklist

- Add a regression proving provider mappings cannot be mutated in place after
  `HostSetupConfig` construction.
- Preserve existing credential-ref validation and secret payload rejection.
- Preserve YAML read/write round-trip behavior for valid host setup config.
- Keep the change localized to `src/awf/host_setup/config.py` and the focused
  host setup config tests.
- Run only targeted tests or checks for the changed behavior.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_host_setup_config.py`
   for in-place mutation of `HostSetupConfig.providers`.
2. Change host setup config mapping fields to immutable mapping instances after
   Pydantic validation.
3. Confirm model serialization still produces YAML-safe plain mappings.
4. Run the focused host setup config tests needed for this change.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Passes with the new regression and existing host setup config coverage.
- Full AWF/GitHub validation is intentionally not run in the agent phase per the
  workspace contract.
