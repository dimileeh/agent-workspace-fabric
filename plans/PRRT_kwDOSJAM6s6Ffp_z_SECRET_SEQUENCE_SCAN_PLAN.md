# PRRT_kwDOSJAM6s6Ffp_z Secret Sequence Scan Plan

## Problem Statement and Scope

The review thread reports that host setup secret payload scanning does not
traverse sequence containers. Current code already scans `list` and `tuple`,
but still misses other `collections.abc.Sequence` implementations.

Scope is limited to `src/awf/host_setup/config.py`, focused unit coverage in
`tests/unit/service/test_host_setup_config.py`, and this plan/validation record.

## Requirements Checklist

- Add a regression proving a non-`list`/`tuple` sequence containing a
  secret-like value is rejected.
- Generalize `_ensure_no_secret_payload` sequence traversal without treating
  `str`, `bytes`, or `bytearray` as containers.
- Preserve existing sanitized diagnostics and the established `audit.[0]` path
  format asserted by current tests.
- Run focused checks only. Full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add the focused regression test and confirm it fails against the current
   `list`/`tuple` implementation.
2. Update `_ensure_no_secret_payload` to scan non-string `Sequence` containers.
3. Run the focused host setup config regression tests and focused lint for the
   touched files.
4. Write a validation document recording requirement status and command
   evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "secret_payload"`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`

Pass criteria: the new focused regression fails before implementation and the
focused checks pass after implementation. Full AWF/GitHub validation remains
managed by AWF after agent completion.
