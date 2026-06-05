# Address Thread PRRT_kwDOSJAM6s6HBVpz Plan

## Problem Statement

The current MCP workspace log slice redaction masks any leading non-delimited
fragment when an expanded read starts in the middle of a token. That preserves
the T17 setup-secret safety case for very long assignment values, but it also
can hide ordinary long log content with no assignment evidence.

## Scope

- Keep the existing T17 behavior that redacts a slice inside a long
  `TOKEN=`/`PASSWORD=` assignment when enough bounded lookback can reveal the
  assignment prefix.
- Preserve ordinary long non-delimited log content when no secret-assignment
  evidence is found.
- Touch only MCP log-read redaction code, focused MCP tests, and this
  plan/validation evidence.

## Steps

1. Add a focused MCP regression for a long benign token read from the middle.
2. Confirm the regression fails under the current blind leading-fragment mask.
3. Change MCP log reads to perform a bounded extra backward read only when the
   requested slice starts inside an unknown leading fragment.
4. Redact the final byte slice from the wider context without blindly masking
   unknown leading fragments; assignment-pattern redaction handles values whose
   keys are found by the lookback.
5. Run focused MCP tests and lint/type checks for touched files.

## Validation Plan

Focused checks only:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'long_benign_token or pattern_only_secret_assignment'
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

Full AWF/GitHub validation, broad test suites, and coverage gates remain owned
by AWF after the agent phase.
