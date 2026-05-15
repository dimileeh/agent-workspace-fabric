# Review Comment 4454403868 Callback Lifecycle And Post Validation

Plan reference:
`plans/review_comment_4454403868_callback_lifecycle_post_PLAN.md`

## Requirement Status

- Complete: Add focused regression coverage before implementation.
  Evidence: The focused pre-implementation pytest command failed with five
  expected failures covering omitted `extensions`, wait-for backstop timeout,
  missing executor shutdown helper, and missing lifespan cleanup call.
- Complete: Add a callback DNS executor shutdown hook and call it from the
  production FastAPI lifespan shutdown path.
  Evidence: `src/awf/service/callbacks.py` now exposes
  `shutdown_callback_target_validation_executor`; `src/awf/api/app.py` calls it
  in lifespan cleanup. Tests cover direct shutdown and lifespan wiring.
- Complete: Preserve existing test behavior for apps constructed with
  `use_lifespan=False`.
  Evidence: Existing callback API and service tests still construct apps
  without lifespan; the new shutdown call is only in `_lifespan`.
- Complete: Omit `extensions` from `httpx.AsyncClient.post` unless a non-`None`
  extensions mapping is required.
  Evidence: `_httpx_post_json` builds kwargs conditionally, and the fake httpx
  client now records whether the keyword was supplied.
- Complete: Keep HTTPS pinned-IP delivery sending the SNI extension and Host
  header.
  Evidence: Existing pinned HTTPS test now also asserts
  `extensions_supplied=True` with the SNI mapping preserved.
- Complete: Make `asyncio.wait_for` use `remaining_timeout + 1` while still
  passing the exact remaining delivery budget to the poster.
  Evidence: The wait-for regression expects the one-second backstop; existing
  fallback tests continue to assert poster timeouts use remaining budget.
- Complete: Do not switch branches, push, or write a GitHub comment.
  Evidence: Work stayed on the current AWF branch; no push or GitHub write was
  performed.

## Verification Evidence

- Failed before implementation as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_default_httpx_poster_posts_json_with_timeout tests/unit/service/test_callbacks.py::test_default_httpx_poster_uses_no_extensions_for_pinned_http_request tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout tests/unit/service/test_callbacks.py::test_callback_target_validation_executor_shutdown_closes_and_resets tests/unit/api/test_app_lifespan.py::TestLifespan::test_lifespan_shuts_down_callback_target_validation_executor -q`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_default_httpx_poster_posts_json_with_timeout tests/unit/service/test_callbacks.py::test_default_httpx_poster_uses_no_extensions_for_pinned_http_request tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout tests/unit/service/test_callbacks.py::test_callback_target_validation_executor_shutdown_closes_and_resets tests/unit/api/test_app_lifespan.py::TestLifespan::test_lifespan_shuts_down_callback_target_validation_executor -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'default_httpx_poster or validated_address_post_attempt_uses_remaining_wall_clock_timeout or drain_due_offloads_callback_target_validation'`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_app_lifespan.py -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py src/awf/api/app.py tests/unit/service/test_callbacks.py tests/unit/api/test_app_lifespan.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf`
- Passed:
  `git diff --check`

## Gaps

None.
