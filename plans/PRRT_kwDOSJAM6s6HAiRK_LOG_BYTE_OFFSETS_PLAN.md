# PRRT_kwDOSJAM6s6HAiRK Log Byte Offsets Plan

## Problem Statement And Scope

The MCP `awf_read_workspace_log` tool accepts byte-based `offset` and `limit_bytes`
parameters, but its redacted slice currently passes byte deltas as Python string
indices. If decoded log text contains multi-byte UTF-8 before the requested
window, the returned text can come from the wrong part of the expanded read.

Scope is limited to fixing the MCP log-read byte window behavior raised in
review thread `PRRT_kwDOSJAM6s6HAiRK`, plus focused regression coverage.

## Requirements Checklist

- Verify the reviewer claim against the current code.
- Preserve the public byte offset contract for `awf_read_workspace_log`.
- Preserve existing secret redaction behavior for slices that overlap configured
  secrets.
- Add focused regression coverage for a multi-byte UTF-8 prefix before the
  requested byte window.
- Run only targeted validation for changed behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add a focused failing MCP regression for reading a requested byte window after
   a multi-byte UTF-8 prefix.
2. Implement byte-aware redacted slicing so byte offsets are translated before
   rendering returned log text.
3. Add or adjust focused redaction-helper coverage if the implementation adds a
   helper.
4. Run targeted pytest commands for the changed MCP and redaction tests.
5. Write validation evidence in the matching validation document.
6. Stage only changed files and commit with a thread-specific conventional
   commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k "read_workspace_log"`
  - Passes and includes the new multi-byte prefix regression.
- If a common redaction helper is changed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k "slice"`
  - Passes existing slice redaction regressions and any new helper coverage.

Full AWF/GitHub validation is intentionally not run in the agent phase.
