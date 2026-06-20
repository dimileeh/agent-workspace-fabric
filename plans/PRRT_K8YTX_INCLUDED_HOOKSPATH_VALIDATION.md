# PRRT_K8YTX Included HooksPath Validation

Plan: `plans/PRRT_K8YTX_INCLUDED_HOOKSPATH_PLAN.md`

## Requirement Status

- Add a regression test showing an included `core.hooksPath` is detected and no
  longer trusted as clean: Complete.
  - Added mirror and linked-worktree include regressions in
    `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
- Update the repair path to inspect included config values: Complete.
  - `_repair_hooks_path_config()` now probes with `--includes --show-origin`
    so included hook-path values are visible.
- Ensure the repair leaves Git lookup clean, either by removing the included
  hook-path value or the include that exposes it: Complete.
  - Direct hook-path entries are still unset directly. Included hook-path
    origins remove the matching `include.path` entry from the inspected config,
    followed by a fail-closed reprobe.
- Preserve existing direct `core.hooksPath` repair behavior: Complete.
  - Existing direct mirror and worktree repair tests remain green.

## Evidence

- Changed `src/awf/node/git_manager.py`.
- Changed `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
- Added this validation document and
  `plans/PRRT_K8YTX_INCLUDED_HOOKSPATH_PLAN.md`.

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py -q
uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py
uv run --python 3.12 --extra dev mypy src/awf/node/git_manager.py
```

Results:

- `19 passed in 0.81s`
- `ruff`: all checks passed
- `mypy`: success

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, and merge gating after completion.
