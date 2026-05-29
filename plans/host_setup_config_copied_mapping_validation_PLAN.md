# Host Setup Config Copied Mapping Validation Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6FjYxz` reports that `write_host_setup_config()`
calls `model_dump()` on copied `HostSetupConfig` instances before revalidating
them. If `model_copy(update=...)` replaces `providers` or `clients` with a
non-mapping object, the field serializer raises a raw Pydantic serialization
error instead of the reason-coded `HostSetupConfigError` contract.

Scope is limited to host setup config write validation and focused unit
coverage for top-level copied mapping replacements.

## Requirements Checklist

- Add regression coverage for copied configs where `providers` is replaced by a
  non-mapping object.
- Add regression coverage for copied configs where `clients` is replaced by a
  non-mapping object.
- Preserve existing handling for invalid entries inside otherwise valid
  mappings.
- Ensure `write_host_setup_config()` raises sanitized `HostSetupConfigError`
  diagnostics before any YAML write for these copied invalid fields.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for the
  changed behavior.

## Implementation Steps

1. Add a focused parametrized regression test in
   `tests/unit/service/test_host_setup_config.py`.
2. Confirm the new regression fails against the current implementation.
3. Change `write_host_setup_config()` to build a raw payload from model fields
   and revalidate it before invoking serializer-backed `model_dump()`.
4. Run the focused host setup config tests that cover the new and nearby
   existing write validation behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "model_copy_updates or copied_mapping"`
  - Passes after implementation.
  - Initially shows the new regression failing before implementation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
