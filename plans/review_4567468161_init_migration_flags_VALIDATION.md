# Review 4567468161 Init Migration Flags Validation

Plan reference:
`plans/review_4567468161_init_migration_flags_PLAN.md`

## Requirement Status

- Complete: Preserve `awf init <path>` project onboarding behavior unchanged.
  Evidence: Changes are limited to the no-path migration branch in
  `src/awf/cli/main.py`; project-path onboarding dispatch and arguments remain
  unchanged.
- Complete: Preserve legacy bootstrap-only flag rejection and labeling for
  `--write-env`, `--no-write-env`, `--timeout-seconds`,
  `--poll-interval-seconds`, `--skip-agent-runtime-build`, and `--provider`.
  Evidence: Legacy no-path flags are now collected in `legacy_flags` and still
  populate `rejected_flags` in JSON plus the legacy pretty-output line.
- Complete: Report no-path project-onboarding flags as requiring a project
  path, not as legacy/deprecated bootstrap flags.
  Evidence:
  `test_init_without_path_rejects_project_mode_flags_as_path_required` covers
  all active project-mode flags and asserts the legacy label is absent.
- Complete: Preserve stable JSON migration payload shape for legacy flags while
  exposing project-mode rejected flags separately.
  Evidence: Existing legacy JSON coverage still asserts
  `rejected_flags == ["--timeout-seconds"]`; new project-mode JSON coverage
  asserts `path_required_flags == ["--write-profile"]` and no `rejected_flags`.
- Complete: Align pretty-mode migration tests to assert `stdout == ""` and
  inspect `stderr`.
  Evidence:
  `test_init_without_path_rejects_unknown_provider_without_traceback` now uses
  explicit stdout/stderr assertions.

## Files Changed

- `src/awf/cli/main.py`
- `tests/unit/cli/test_init_parts/test_init_part_001.py`
- `tests/unit/cli/test_init_parts/test_init_part_004.py`
- `plans/review_4567468161_init_migration_flags_PLAN.md`
- `plans/review_4567468161_init_migration_flags_VALIDATION.md`

## Verification Evidence

Failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_without_path_rejects_unknown_provider_without_traceback -q
```

Result: failed as expected for the new project-mode flag wording assertions
(`7 failed, 1 passed`).

Passing targeted checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_legacy_bootstrap_flags_with_migration tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_reports_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_without_path_rejects_unknown_provider_without_traceback -q
```

Result: `19 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_returns_migration_payload tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_rejects_invalid_legacy_timeout_with_migration -q
```

Result: `2 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init_parts/test_init_part_001.py tests/unit/cli/test_init_parts/test_init_part_004.py
```

Result: `All checks passed!`

Full AWF/GitHub validation was not run inside the agent phase per the workspace
contract; AWF owns broad validation, provenance, logs, and merge gating after
agent completion.
