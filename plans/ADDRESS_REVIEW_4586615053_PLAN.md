# Address Review Comment 4586615053 Plan

## Problem Statement and Scope

PR review comment `issue:4586615053` reports remaining Cursor runtime cleanup
items in the agent-runtime Dockerfile and Cursor adapter tests. The scoped
work is limited to:

- `docker/agent-runtime.Dockerfile`
- `src/awf/adapters/cursor.py`
- focused Cursor/Dockerfile unit tests
- this plan and its validation artifact

The existing branch already has the root `cursor-agent --version || true`
smoke-check change, but the Node symlink still uses the unresolved
`command -v node` path and the Cursor adapter still exposes an unused private
delegating helper.

## Requirements Checklist

- Preserve the soft root-level Cursor version smoke check:
  `cursor-agent --version || true`.
- Preserve the strict non-root Cursor version check after `USER agent`.
- Canonicalize the Node binary source before symlinking it into
  `/usr/local/bin/node` so the Dockerfile cannot create a self-referential
  symlink.
- Remove the unused private `_cursor_model_for_effort` wrapper from
  `src/awf/adapters/cursor.py`.
- Keep Cursor effort-mapping tests pointed at the shared model-selection helper.
- Do not run broad AWF/GitHub-owned validation; use focused local checks only.

## Implementation Steps

1. Update focused regression assertions in
   `tests/unit/test_agent_runtime_dockerfile.py` for the canonicalized Node
   symlink and split Cursor version-check behavior.
2. Update the Cursor adapter effort-mapping test to import
   `cursor_model_for_effort` directly from `awf.adapters.model_selection`.
3. Run the focused Dockerfile test before the Dockerfile edit and confirm the
   new canonicalized symlink assertion fails.
4. Update `docker/agent-runtime.Dockerfile` to use `readlink -f` around
   `command -v node`.
5. Remove the unused private Cursor helper and stale import from
   `src/awf/adapters/cursor.py`.
6. Run the focused Dockerfile and Cursor adapter tests.
7. Create the validation artifact with requirement-by-requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Expected to fail after the test-only edit and before the Dockerfile fix.
  - Expected to pass after the Dockerfile fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
  - Expected to pass after the import/helper cleanup.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
