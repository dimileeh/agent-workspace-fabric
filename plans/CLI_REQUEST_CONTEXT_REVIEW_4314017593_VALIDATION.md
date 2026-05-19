# CLI Request Context Review 4314017593 Validation

Plan reference: `CLI_REQUEST_CONTEXT_REVIEW_4314017593_PLAN.md`

## Requirement Status

- Remove the module-global request context dictionary used to map response IDs
  back to request metadata: Complete. `_CALL_CONTEXT` was removed from
  `src/awf/cli/main.py`.
- Preserve sanitized HTTP method and URL context in CLI error output: Complete.
  `_request_context` now reads `httpx.Response.request` and the existing error
  output tests still assert sanitized method/URL context.
- Keep token-bearing headers and sensitive URL parts out of stderr: Complete.
  Existing token-redaction and query-redaction tests continue to pass.
- Add or update regression coverage so request context is carried by
  `httpx.Response.request`, not leaked through global state: Complete.
  `test_handle_response_uses_response_request_without_global_context` asserts
  the old cache is absent.
- Commit the local fix with a conventional commit referencing comment
  4314017593: Complete. The local commit uses
  `fix: address review comment 4314017593 - remove CLI request context cache`.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_cli.py`
- `plans/CLI_REQUEST_CONTEXT_REVIEW_4314017593_PLAN.md`
- `plans/CLI_REQUEST_CONTEXT_REVIEW_4314017593_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::test_handle_response_uses_response_request_without_global_context -q`
  failed before implementation with `_CALL_CONTEXT` still present, then passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/common/test_urls.py -q`
  passed: 128 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py tests/unit/common/test_urls.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Remaining Gaps

No planned requirement gaps remain.
