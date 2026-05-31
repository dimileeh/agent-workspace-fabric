# Address Review Comment 4586615053 Validation

Plan reference: `plans/ADDRESS_REVIEW_4586615053_PLAN.md`

## Requirement Status

- Preserve the soft root-level Cursor version smoke check:
  `cursor-agent --version || true`.
  - Complete. `docker/agent-runtime.Dockerfile` keeps the npm-block smoke
    check tolerant of version-command failure.
- Preserve the strict non-root Cursor version check after `USER agent`.
  - Complete. The Dockerfile contract test still asserts the final non-root
    `cursor-agent --version` check without `|| true`.
- Canonicalize the Node binary source before symlinking it into
  `/usr/local/bin/node`.
  - Complete. The Dockerfile now symlinks from
    `$(readlink -f "$(command -v node)")`.
- Remove the unused private `_cursor_model_for_effort` wrapper.
  - Complete. `src/awf/adapters/cursor.py` no longer defines or imports the
    delegating wrapper.
- Keep Cursor effort-mapping tests pointed at the shared model-selection helper.
  - Complete. `tests/unit/adapters/test_adapters.py` imports
    `cursor_model_for_effort` from `awf.adapters.model_selection`.
- Do not run broad AWF/GitHub-owned validation.
  - Complete. Only focused unit and lint checks listed below were run. Full
    AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `src/awf/adapters/cursor.py`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `tests/unit/adapters/test_adapters.py`
- `plans/ADDRESS_REVIEW_4586615053_PLAN.md`
- `plans/ADDRESS_REVIEW_4586615053_VALIDATION.md`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Failed after the test-only edit, before the Dockerfile fix, because the
    Dockerfile still used `ln -sf "$(command -v node)" /usr/local/bin/node`.
  - Passed after the Dockerfile fix: `1 passed in 0.37s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
  - Passed: `1 passed in 0.44s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/cursor.py tests/unit/adapters/test_adapters.py tests/unit/test_agent_runtime_dockerfile.py`
  - Passed: `All checks passed!`.

## Remaining Gaps

None for the scoped review comment. Full image-build and CI-equivalent
validation were intentionally not run in the agent phase.
