# Review 4567468161 Init Migration Flags Plan

## Problem Statement And Scope

PR review comment `issue:4567468161` identified two narrow issues in the
public `awf init` migration behavior:

- No-path project-onboarding flags such as `--write-profile` are currently
  reported as "legacy" flags, even though they remain valid when a project path
  is supplied.
- One migration-error regression test reads `result.output` instead of asserting
  the intended stderr-only pretty output contract.

Scope is limited to the `awf init` no-path migration error handling and the
targeted CLI regression tests. No AWF/GitHub-owned broad validation will be run
inside this agent phase.

## Requirements Checklist

- Preserve `awf init <path>` project onboarding behavior unchanged.
- Preserve legacy bootstrap-only flag rejection and labeling for:
  `--write-env`, `--no-write-env`, `--timeout-seconds`,
  `--poll-interval-seconds`, `--skip-agent-runtime-build`, and `--provider`.
- Report no-path project-onboarding flags as requiring a project path, not as
  legacy/deprecated bootstrap flags.
- Preserve stable JSON migration payload shape for legacy flags while exposing
  project-mode rejected flags separately if needed.
- Align pretty-mode migration tests to assert `stdout == ""` and inspect
  `stderr`.

## Implementation Steps

1. Add or update focused tests that fail against the current implementation:
   one for no-path project-mode flag wording/payload, and one for explicit
   stderr assertions on the unknown-provider migration error.
2. Run the targeted failing tests where practical to confirm the regression.
3. Split no-path rejected flags in `src/awf/cli/main.py` into project-mode and
   legacy-bootstrap categories, then update the migration error emitter to print
   separate path-focused and legacy-focused messages.
4. Run targeted unit tests for the changed CLI behavior only.
5. Record validation evidence in
   `plans/review_4567468161_init_migration_flags_VALIDATION.md`.
6. Stage only changed files and commit locally with a conventional commit
   referencing review comment `4567468161`.

## Verification Commands And Pass Criteria

- Targeted failure check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_without_path_rejects_unknown_provider_without_traceback -q`
- Targeted final check:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_legacy_bootstrap_flags_with_migration tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_rejects_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_without_path_json_reports_project_mode_flags_as_path_required tests/unit/cli/test_init_parts/test_init_part_004.py::test_init_without_path_rejects_unknown_provider_without_traceback -q`
- Pass criteria: targeted tests pass, stderr/stdout contracts are explicit, and
  broad AWF/GitHub validation remains delegated to AWF after agent completion.
