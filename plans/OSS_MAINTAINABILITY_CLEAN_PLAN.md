# OSS Maintainability Clean Plan

## Summary

Make AWF's first-party codebase maintainability-clean under a hard 1,500-line
file limit. This is a no-behavior-change refactor: split oversized source,
console, script, unit-test, and integration-test files into focused modules,
remove catch-all shared barrels, and enforce the rule with a repo-wide guard.

## Baseline

At the start of this pass, 60 first-party code files exceeded 1,500 lines under
`src/`, `tests/`, and `apps/console` first-party app folders. Vendor/build/cache
folders are excluded.

Largest offenders:

- `tests/unit/control/test_worker.py`: 21,818 lines
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`: 8,578 lines
- `tests/unit/control/test_quality_gates.py`: 6,735 lines
- `tests/unit/control/test_executor_coverage_edges.py`: 6,063 lines
- `tests/unit/runtime/test_pr_monitor_runner.py`: 5,769 lines
- `apps/console/components/console-dashboard.tsx`: 4,933 lines
- `src/awf/cli/main.py`: 3,656 lines
- `src/awf/service/workspaces.py`: 3,174 lines
- `src/awf/control/quality_gates.py`: 3,009 lines
- `src/awf/runtime/validation.py`: 2,811 lines

## Key Changes

- Replace scoped decomposition guard with a repo-wide first-party
  maintainability guard covering Python, TypeScript, and JavaScript files.
- Remove remaining catch-all shared modules in the decomposed core packages;
  replace them with narrow config, constants, types, protocols, and helper
  modules with explicit imports.
- Split oversized production/source files by domain and keep existing public
  facades stable.
- Split oversized test files by scenario family or existing test classes, with
  shared fixtures in focused helper modules.
- Preserve AWF behavior exactly: no scheduling, API, PR monitor, validation,
  console, or runtime policy changes.

## Validation

Run the maintainability guard before cleanup to confirm it fails on the
baseline, then after cleanup to confirm it passes. Final validation:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests scripts
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
uv run --python 3.12 --extra dev pytest tests/unit -q -n 20
uv run --python 3.12 --extra dev pytest tests/integration -q -n 20
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
```

## Assumptions

- The hard limit is 1,500 lines for every first-party `.py`, `.ts`, `.tsx`,
  `.js`, and `.jsx` file.
- Excluded folders are vendor/build/cache artifacts only: `node_modules`,
  `.next`, `__pycache__`, coverage/build artifacts, and similar generated
  outputs.
- Public compatibility remains only for intentional public APIs.
