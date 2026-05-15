# PRRT_kwDOSJAM6s6CNj6H Plan

## Problem Statement And Scope

The setup dependency-network classifier currently requires compound setup command
output to show dependency/index context and transient network context on the same
line. A `uv sync --extra dev && ./bootstrap` failure can split package,
package-index URL, and DNS cause across adjacent lines, causing AWF to skip the
bounded setup retry and report an opaque setup failure.

Scope is limited to the setup dependency-network classifier and focused
regression coverage.

## Requirements Checklist

- Add a regression test for a compound setup command whose `uv` dependency
  failure evidence is split across multiple output lines.
- Preserve existing false-positive protections for compound commands where a
  later bootstrap step, not dependency setup, hits a transient network failure.
- Keep classification metadata intact: reason code, transient category,
  package, and host.
- Do not broaden retries to deterministic setup failures.

## Implementation Steps

1. Add the failing regression in `tests/unit/runtime/test_validation.py`.
2. Run the focused regression and confirm the current classifier misses it.
3. Update `src/awf/runtime/validation.py` to evaluate a bounded failure block
   around transient output lines and require dependency failure context within
   that block.
4. Run focused classifier tests, then a narrow runtime validation test file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- If time permits, `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
