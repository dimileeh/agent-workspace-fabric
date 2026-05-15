# Review Comment 4454403868 Summary Follow-Up Validation

Plan reference: `review_comment_4454403868_summary_followup_PLAN.md`

## Requirement Status

- Keep delivery-time callback target validation rules intact:
  Complete. `_validate_callback_target` still enforces the existing URL,
  HTTPS, allowlist, public-host, and DNS validation rules.
- Add a concise defense-in-depth comment explaining the duplicate
  URL-structure checks in delivery-time validation:
  Complete. `src/awf/service/callbacks.py` now documents that delivery may see
  legacy or manually edited rows and keeps the checks as pre-DNS invariants.
- Add a regression test proving the OpenAPI auth contract patch is applied once
  and cached for later `app.openapi()` calls:
  Complete. `test_openapi_auth_contract_patch_runs_once_after_schema_is_cached`
  failed before implementation with `assert 2 == 1` and passes after the fix.
- Update the OpenAPI wrapper to return the already-patched cached schema on
  subsequent calls:
  Complete. `openapi_with_auth_contract` now returns `app.openapi_schema` when
  it is already populated.
- Do not push, switch branches, or write any GitHub comment:
  Complete. No branch switch, push, or GitHub write was performed.

## Evidence

Files changed:

- `src/awf/api/app.py`
- `src/awf/service/callbacks.py`
- `tests/unit/api/test_openapi_artifact.py`
- `plans/review_comment_4454403868_summary_followup_PLAN.md`
- `plans/review_comment_4454403868_summary_followup_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q -k openapi_auth_contract_patch_runs_once`
  - First run failed before implementation: `assert 2 == 1`.
  - Second run passed: `1 passed, 11 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  - Passed: `12 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'enforces_https_only_callback_target_policy or enforces_callback_target_allowlist_policy'`
  - Passed: `2 passed, 23 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/app.py src/awf/service/callbacks.py tests/unit/api/test_openapi_artifact.py`
  - Passed.
- `python scripts/generate_openapi.py --check`
  - Failed in the bare interpreter because dependencies were unavailable:
    `ModuleNotFoundError: No module named 'fastapi'`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed: `OK: openapi.json matches the current app spec.`
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: `Success: no issues found in 155 source files`.

## Gaps

None.
