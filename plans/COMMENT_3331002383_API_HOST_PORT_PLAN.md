# COMMENT_3331002383 API Host Port Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F-ZyN` reports that the CLI reads
`AWF_API_HOST_PORT` directly when deriving the default host API URL. Invalid
values can produce malformed request URLs and defer the failure to the HTTP
client.

Scope is limited to CLI base URL resolution and focused unit coverage for that
behavior.

## Requirements Checklist

- Reject non-numeric `AWF_API_HOST_PORT` values before any HTTP request is made.
- Reject host ports outside the valid TCP port range `1..65535`.
- Preserve valid `AWF_API_HOST_PORT` behavior and existing precedence for
  `--base-url`, `AWF_BASE_URL`, and deprecated `AWF_CLI_BASE_URL`.
- Report a clear CLI error and exit with code `2` for invalid host ports.
- Use focused tests only; full AWF/GitHub validation remains managed after the
  agent phase.

## Implementation Steps

1. Add a failing CLI unit test beside existing base URL resolution tests for
   invalid `AWF_API_HOST_PORT` values.
2. Add a small parser in `awf.cli.common` for `AWF_API_HOST_PORT`.
3. Use the parser when deriving the default host URL.
4. Run the focused CLI unit tests that cover this behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py -q`
  passes after implementation.
- Before implementation, the new regression test fails because invalid host
  ports are not rejected.
