# Review Comment 4318465127 Plan

## Problem Statement And Scope

CodeRabbit review comment `4318465127` requests stronger regression coverage
for the local service Compose env path work. The comment includes four findings:
two against `tests/unit/cli/test_service_cli.py`, one against
`tests/unit/cli/test_init.py`, and one against
`tests/unit/service/test_bootstrap.py`.

Scope is limited to verifying those findings against the current branch and
making the minimal code/test changes required for still-valid issues. No GitHub
write actions, branch changes, pushes, or unrelated refactors are in scope.

## Requirements Checklist

- Verify each referenced finding against the current code before editing.
- Preserve already-present regression coverage in `test_init.py` and
  `test_bootstrap.py` if those comments are already addressed.
- Add docs regression coverage for both `docs/QUICKSTART.md` and
  `docs/GETTING_STARTED.md` so stale root `.env` wording is caught in either
  document.
- Assert that service bootstrap CLI tests forward the resolved Compose file and
  env file to `run_service_bootstrap()`.
- Assert that service status CLI tests forward the resolved Compose file and env
  file to `collect_service_status()`.
- If status forwarding is missing in production code, add the smallest
  compatible plumbing without changing unrelated status behavior.
- Run targeted tests and lint/type checks appropriate to the touched files.

## Implementation Steps

1. Inspect the referenced tests and service CLI handoffs.
2. Add or update failing assertions for the still-valid service CLI gaps.
3. Update `awf service status` and `collect_service_status()` as needed to
   accept and forward Compose paths.
4. Parameterize the docs guard over `QUICKSTART.md` and `GETTING_STARTED.md`.
5. Run the focused tests first, then run lint/type checks for touched code.

## Verification Commands And Pass Criteria

- Initial focused TDD command should fail on the missing status forwarding after
  the new assertions are added.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_bootstrap_cli_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_status_resolves_settings_from_compose_env tests/unit/cli/test_service_cli.py::test_service_status_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_status_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_readme_documents_compose_env_bootstrap_path -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/status.py src/awf/cli/main.py tests/unit/cli/test_service_cli.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/service/status.py src/awf/cli/main.py`
  passes.
