# PR #342 CI Fix Validation

Plan reference: `plans/PR_342_CI_FIX_PLAN.md`

## Requirement Status

- Preserve the Cursor CLI installer path and do not weaken the `cursor-agent
  --version` smoke checks: Complete. The installer remains
  `curl https://cursor.com/install -fsS | HOME=/opt/cursor bash`, and both root
  and non-root `cursor-agent --version` checks remain strict.
- Make `/usr/local/bin/node` available in the agent-runtime image before
  `cursor-agent` is executed: Complete. `docker/agent-runtime.Dockerfile`
  symlinks `$(command -v node)` to `/usr/local/bin/node` during the NodeSource
  install stage and verifies it is executable before any Cursor step runs.
- Add or update focused regression coverage for the Dockerfile behavior:
  Complete. `tests/unit/test_agent_runtime_dockerfile.py` now asserts the
  `/usr/local/bin/node` symlink exists and is ordered before the Cursor
  installer.
- Run only focused local checks: Complete. No full coverage, full repository
  test suite, frontend build, or CI-equivalent Docker image build was run
  locally.
- Commit the local fix without pushing or changing branches: Complete. The
  staged fix is committed locally as part of this CI-fix cycle, and no push or
  branch change is performed.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_342_CI_FIX_PLAN.md`
- `plans/PR_342_CI_FIX_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - First run failed before implementation on the missing
    `/usr/local/bin/node` symlink assertion.
  - Second run passed after the Dockerfile fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present -q`
  - Passed: 8 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  - Passed: 7 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_provider_readiness_all_green tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  - Passed.
- `git diff --check`
  - Passed.

## Deferred Validation

Full AWF/GitHub validation, including the actual CI Docker image build,
`python-full-coverage`, `release-artifacts`, broad coverage, and frontend
checks, is managed by AWF/GitHub after agent completion per the workspace
contract.

## Gaps

No planned implementation gaps remain.
