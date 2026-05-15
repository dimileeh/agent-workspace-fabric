# Review Comment 4454403868 Callback Lifecycle And Post Plan

## Problem Statement and Scope

PR #249 received a review-level callback-security follow-up with three
actionable implementation nits:

- The callback DNS validation executor is a module-level singleton and should
  have an explicit application shutdown path.
- `_httpx_post_json` should omit the `extensions` keyword when no pinned HTTPS
  SNI override is needed.
- `_post_to_validated_callback_addresses` should let the poster/httpx timeout
  be the primary signal and use `asyncio.wait_for` only as a slightly longer
  coroutine backstop.

Scope is limited to callback delivery helpers, FastAPI lifespan teardown,
focused tests, and this plan/validation record.

## Requirements Checklist

- Add focused regression coverage before implementation.
- Add a callback DNS executor shutdown hook and call it from the production
  FastAPI lifespan shutdown path.
- Preserve existing test behavior for apps constructed with
  `use_lifespan=False`.
- Omit `extensions` from `httpx.AsyncClient.post` unless a non-`None`
  extensions mapping is required.
- Keep HTTPS pinned-IP delivery sending the SNI extension and Host header.
- Make `asyncio.wait_for` use `remaining_timeout + 1` while still passing the
  exact remaining delivery budget to the poster.
- Do not switch branches, push, or write a GitHub comment.

## Implementation Steps

1. Update service and lifespan tests to express the new expected behavior.
2. Run focused tests to confirm the new expectations fail on the current code.
3. Add a public callback executor shutdown helper in `src/awf/service/callbacks.py`.
4. Wire the shutdown helper into `src/awf/api/app.py` lifespan cleanup.
5. Update `_httpx_post_json` and `_post_to_validated_callback_addresses`.
6. Run focused tests, then narrow lint/type checks for touched files.
7. Record validation evidence in
   `plans/review_comment_4454403868_callback_lifecycle_post_VALIDATION.md`.
8. Stage only changed files and commit locally with the requested
   review-comment fix message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_default_httpx_poster_posts_json_with_timeout tests/unit/service/test_callbacks.py::test_default_httpx_poster_uses_no_extensions_for_pinned_http_request tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout tests/unit/api/test_app_lifespan.py::TestLifespan::test_lifespan_shuts_down_callback_target_validation_executor -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'default_httpx_poster or validated_address_post_attempt_uses_remaining_wall_clock_timeout or drain_due_offloads_callback_target_validation'`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_app_lifespan.py -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py src/awf/api/app.py tests/unit/service/test_callbacks.py tests/unit/api/test_app_lifespan.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
