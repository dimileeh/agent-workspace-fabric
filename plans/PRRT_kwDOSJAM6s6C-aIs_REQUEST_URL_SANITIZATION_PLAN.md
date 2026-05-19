# PRRT_kwDOSJAM6s6C-aIs Request URL Sanitization Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6C-aIs` flagged the CLI request URL sanitizer in
`src/awf/cli/main.py`. The helper manually reconstructs host and port, which can
change valid authorities such as IPv6 literals, and it lives in the CLI module
instead of a shared URL utility.

## Requirements Checklist

- Move request URL sanitization into shared URL utilities.
- Preserve URL authority via `urlsplit().netloc` rather than manual host/port
  reconstruction.
- Continue redacting sensitive query parameter values in operator-facing error
  messages.
- Avoid emitting pairs with `None` values when rebuilding the query string.
- Preserve existing CLI request-context behavior.
- Keep changes scoped to the review-thread issue.

## Implementation Steps

1. Add focused tests for the shared sanitizer covering secret query redaction,
   IPv6 authority preservation, malformed/relative URL passthrough, and URL
   userinfo redaction.
2. Implement the shared sanitizer in `awf.common.urls`.
3. Update the CLI to import and use the shared sanitizer.
4. Run focused URL and CLI tests plus lint on touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_urls.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/urls.py src/awf/cli/main.py tests/unit/common/test_urls.py tests/unit/cli/test_cli.py`

All commands should pass.
