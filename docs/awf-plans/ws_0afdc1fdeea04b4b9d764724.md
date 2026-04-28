# Implementation Plan: Fix AWF Plan Guard for Committed Plans

## Root Cause

`_run_agent_task_with_optional_planning` in `src/awf/control/executor.py` checks whether the planning phase produced the required plan file by calling `_changed_paths`, which runs `git status --porcelain`. If an agent commits the plan file during planning, the worktree becomes clean and `_changed_paths` returns an empty set. The guard then incorrectly reports:

> "planning phase did not create or modify required plan file ..."

This strand committed work and fails the workspace even though the plan artifact exists.

## Intended Files/Modules to Touch

- `src/awf/control/executor.py`
  - Modify `_run_agent_task_with_optional_planning` to detect plan-path changes from **committed** files in addition to dirty/untracked files.
  - Modify `_changed_paths` or add a new helper `_committed_paths_since` that returns paths changed between a starting commitish and HEAD.
- `tests/unit/control/test_executor_coverage_edges.py`
  - Add focused unit tests for the new behavior.

## Tests to Write First (TDD)

1. **committed-plan accepted**
   - Agent writes and commits the plan file during planning.
   - `git status --porcelain` returns empty.
   - Guard detects the committed plan via `git diff --name-only <base>..HEAD` and allows execution to proceed.

2. **committed-plan-plus-code rejected as outside-plan**
   - Agent commits the plan file *and* a source file during planning.
   - `enforce_plan_only_changes=true`.
   - Guard detects the extra committed file and fails with the *outside-plan* message, not the misleading missing-plan message.

3. **dirty plan file still accepted**
   - Agent leaves the plan file as an untracked/dirty file (original happy path).
   - Guard continues to accept it.

4. **dirty extra file still rejected**
   - Agent leaves plan file plus an extra dirty file.
   - `enforce_plan_only_changes=true`.
   - Guard continues to reject with the outside-plan message.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

If coverage report is needed after touching core behavior:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Detailed Fix Design

### Step 1: Snapshot the pre-planning baseline

Before invoking the planning adapter, capture the current HEAD SHA:

```python
baseline_sha = await _git_in_worktree(["rev-parse", "HEAD"])
```

If the SHA cannot be resolved, fall through to the existing `_changed_paths`-only behavior (worktree is likely fresh).

### Step 2: Union committed and dirty changes

After the planning adapter returns, collect:

- `dirty_paths = await self._changed_paths(worktree_path)`  # porcelain output
- `committed_paths = await self._committed_paths(worktree_path, since=baseline_sha)`  # new helper

The helper runs:

```bash
git diff --name-only <baseline_sha>..HEAD
```

and returns a `set[Path]` of repo-relative changed paths. If the command fails (e.g. fresh repo with no commits), return an empty set.

### Step 3: Combined plan-path check

Use the union for the plan-file check:

```python
planning_changes = dirty_paths | committed_paths
if plan_path not in planning_changes:
    return "planning phase did not create or modify required plan file ..."
```

### Step 4: Combined outside-plan check

For `enforce_plan_only_changes`, the set of *new* changes introduced during planning is:

```python
extra = sorted(planning_changes - {plan_path})
```

This detects both:
- Dirty untracked/modified files outside the plan path (existing behavior), and
- Committed files outside the plan path (new behavior).

### Step 5: Preserve conformance-phase deviation check

The post-execution / post-compare checks already use `_changed_paths` (porcelain). They are **not** affected by commits made during planning because the planning-phase commits happened before execution. The execution and compare phases may add their own dirty files, which porcelain continues to capture correctly. No changes needed there.

## Risks and Assumptions

- Assumption: The worktree has a valid git history and `HEAD` resolves before planning. If not, the committed-paths helper returns empty set and behavior degrades to the old porcelain-only check (safe fallback).
- Assumption: Agents do not rewrite history during planning (e.g. `git rebase`). If they do, `diff --name-only` still reports changed paths, which is acceptable for the guard.
- Risk: `git diff --name-only <sha>..HEAD` with a large number of commits is fast and safe; it only lists filenames.
- Risk: If the baseline SHA is the same as HEAD after planning (no commits), the diff returns empty, and we rely on porcelain. This is correct.

## Explicit Non-Goals

- Do not add git-history-rewriting detection or recovery.
- Do not change the conformance-phase deviation logic.
- Do not weaken or lower coverage thresholds, `fail_under`, profile coverage requirements, or PRD quality gates.
- Do not modify docs, migrations, lockfiles, or config other than the requested plan file.

## Commit Message

```
fix(executor): planning guard now detects committed plans

Root cause: _run_agent_task_with_optional_planning only checked
`git status --porcelain`, so if an agent committed the plan file
during planning the worktree was clean and AWF falsely reported
"planning phase did not create or modify required plan file".

Fix: capture the pre-planning HEAD SHA, then union committed
changes (`git diff --name-only <base>..HEAD`) with dirty/untracked
changes (porcelain) when checking for the plan artifact and when
enforcing plan-only changes. A model that commits code during the
planning phase now gets the correct "outside plan" error instead
of the misleading "missing plan" error.

Refs: ws_15fcdd21401d4d0495747d03
```