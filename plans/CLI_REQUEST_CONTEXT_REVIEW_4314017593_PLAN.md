# CLI Request Context Review 4314017593 Plan

## Problem Statement and Scope

PR review comment 4314017593 flags a potential memory leak in the AWF CLI's
module-level response request context cache. The fix is scoped to CLI HTTP
response context handling and the focused tests that pin operator-facing error
messages. URL helper refactors are out of scope unless needed for the leak fix.

## Requirements Checklist

- Remove the module-global request context dictionary used to map response IDs
  back to request metadata.
- Preserve sanitized HTTP method and URL context in CLI error output.
- Keep token-bearing headers and sensitive URL parts out of stderr.
- Add or update regression coverage so request context is carried by
  `httpx.Response.request`, not leaked through global state.
- Commit the local fix with a conventional commit referencing comment
  4314017593.

## Implementation Steps

1. Update the CLI regression tests first to expect request context from
   `httpx.Response.request` and to reject the old global cache.
2. Run the focused test and confirm it fails against the current implementation.
3. Remove `_CALL_CONTEXT` from `src/awf/cli/main.py`.
4. Read request metadata directly from `httpx.Response.request`, handling
   responses without request metadata defensively.
5. Preserve test support for mocked HTTP responses by attaching request metadata
   to the response object rather than storing it globally.
6. Run the focused CLI/common URL tests, then lint/typecheck for touched Python
   code as practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/common/test_urls.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py tests/unit/common/test_urls.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
