# Comment 4460873446 Validation

Plan reference: `plans/COMMENT_4460873446_PLAN.md`

## Requirement Status

- Complete: Workspace v1/v2 idempotency-key 429 responses use a fresh request-admission decision after durable replay miss.
  - Evidence: `tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_refreshes_preview_after_durable_miss`.
  - Files: `src/awf/api/routes/workspaces.py`.

- Complete: Callback fresh registration path keeps cold durable replay bypass semantics while avoiding repeated advisory-lock acquisition for the same fresh key.
  - Evidence: `tests/unit/api/test_callbacks.py::test_register_callback_fresh_path_acquires_one_idempotency_lock` and updated locked-session replay assertions.
  - Files: `src/awf/api/routes/callbacks.py`, `src/awf/service/callbacks.py`, `src/awf/db/repositories.py`.

- Complete: `settings_guardrails` has no silently discarded `callbacks_enabled` parameter and callers compile against the new signature.
  - Evidence: `tests/unit/service/test_config.py::test_settings_guardrails_rejects_removed_callbacks_enabled_argument`.
  - Files: `src/awf/common/config.py`, `tests/unit/service/test_config.py`.

- Complete: `admit_request(None, ...)` callers do not share limiter buckets across calls.
  - Evidence: `tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter`.
  - Files: `src/awf/api/request_admission.py`, `tests/unit/api/test_deps.py`.

- Complete: Narrow tests for touched API/config/request-admission behavior pass.
  - Evidence: focused and full validation commands below.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter tests/unit/service/test_config.py::test_settings_guardrails_rejects_removed_callbacks_enabled_argument tests/unit/api/test_callbacks.py::test_register_callback_fresh_path_acquires_one_idempotency_lock tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_refreshes_preview_after_durable_miss -q`
  - Result: failed before implementation, then passed after implementation (`5 passed`).

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss -q`
  - Result: passed after updating the existing callback seam assertion to the locked-session path.

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/service/test_config.py tests/unit/service/test_callbacks.py tests/unit/db/test_callback_repository.py -q`
  - Result: passed (`398 passed`).

- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py src/awf/common/config.py src/awf/service/callbacks.py src/awf/db/repositories.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/service/test_config.py`
  - Result: passed.

- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed (`Success: no issues found in 158 source files`).

- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed.

- `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Result: passed (`6603 passed`).

## Gaps

No remaining gaps.
