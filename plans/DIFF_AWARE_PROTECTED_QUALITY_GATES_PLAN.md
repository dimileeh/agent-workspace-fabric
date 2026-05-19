# Diff-Aware Protected Quality Gates Plan

## Plan Reference

Implementation follows the AWF workspace contract in
`docs/awf-plans/ws_285b5bf215fd4b329eb1af65.md`.

## Problem And Scope

Replace path-only blocking for unowned protected quality-gate files with a
deterministic local-diff classifier for `pyproject.toml` and GitHub workflow
YAML files. Preserve fail-closed behavior when local old/new content is missing
or cannot be parsed, and preserve the existing `owned_paths` override.

This is not a branch-protection API integration and not a general policy engine.

## Requirements Checklist

- Add TDD coverage for allowed dependency additions and workflow pinned-action
  bumps.
- Block lowered coverage thresholds, dependency deletions, unsafe workflow
  `continue-on-error`, removed workflow jobs/steps, gate edits, and validation
  command narrowing.
- Include file, section/path, approximate line, and reason in quality-gate
  violation messages.
- Collect local protected-file old/new content in executor commit checks.
- Collect local protected-file old/new content in PR monitor dirty and pre-push
  checks.
- Document protected-file allowed and blocked edits plus operator override paths.

## Implementation Steps

1. Extend `QualityGateViolation` and add a `ProtectedFileDiff` input model.
2. Add semantic classifiers for `pyproject.toml` and `.github/workflows/*.yml`
   or `.yaml`.
3. Keep path-level blocking for protected files outside that classifier.
4. Wire executor staged and committed checks to pass protected-file diff inputs.
5. Wire PR monitor dirty, unpushed, and sync-base push checks to pass
   protected-file diff inputs and richer event details.
6. Add operator documentation in `docs/PROTECTED_FILES.md`.

## Verification

Pass the task-specified commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py
uv run --python 3.12 --extra dev mypy src/awf
```
