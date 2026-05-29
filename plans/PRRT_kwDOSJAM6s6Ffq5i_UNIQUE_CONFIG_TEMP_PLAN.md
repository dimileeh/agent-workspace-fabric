# PRRT_kwDOSJAM6s6Ffq5i Unique Config Temp Plan

## Problem Statement And Scope

`write_host_setup_config` currently writes through a fixed sibling temp path before
renaming it over the target config. Concurrent writes to the same config path can
truncate or replace the same temp file. Scope is limited to host setup config
write temp path generation and focused unit coverage.

## Requirements Checklist

- Add a regression test proving repeated writes use distinct sibling temp paths.
- Keep atomic replacement behavior and conservative permissions intact.
- Remove only temp files created by the failing write attempt.
- Avoid broad validation; AWF/GitHub own full validation after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_host_setup_config.py` that
   records temp paths opened by two writes to the same config path and requires
   them to differ.
2. Confirm the new test fails against the fixed `.config.yml.tmp` behavior.
3. Update `write_host_setup_config` to generate a per-write unique sibling temp
   filename.
4. Run the focused host setup config unit test file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passes.
- Full AWF/GitHub validation is intentionally left to AWF after agent completion.
