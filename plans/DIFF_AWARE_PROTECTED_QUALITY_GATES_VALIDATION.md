# Diff-Aware Protected Quality Gates Validation

Plan reference: `plans/DIFF_AWARE_PROTECTED_QUALITY_GATES_PLAN.md`

Original implementation contract:
`docs/awf-plans/ws_285b5bf215fd4b329eb1af65.md`

## Requirement Status

- Complete: Replace path-only blocking with a diff-aware classifier for
  `pyproject.toml` and GitHub workflow YAML, with fail-closed behavior when old
  or new content is unavailable or unparsable.
- Complete: Preserve existing `owned_paths` override behavior.
- Complete: Allow pyproject dependency additions and metadata-only edits while
  blocking dependency deletions, protected tool/build policy edits, and lowered
  coverage thresholds.
- Complete: Allow workflow comment/notify `continue-on-error`, pinned
  `uses:` ref bumps, and informational jobs while blocking unsafe
  `continue-on-error`, removed jobs/steps, gate `if:` edits, and validation
  command changes.
- Complete: Improve violation messages and event payloads with file,
  section/path, approximate line, and reason.
- Complete: Add operator documentation in `docs/PROTECTED_FILES.md`.
- Complete: Add executor and PR monitor regression coverage for allowed and
  blocked protected edits.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `src/awf/control/executor.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/control/test_quality_gates.py`
- `tests/unit/control/test_executor_validation_fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `docs/PROTECTED_FILES.md`

Validation commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
# 186 passed in 107.65s

uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py
# All checks passed.

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 156 source files
```

## Gaps

None identified for this P1 slice.
