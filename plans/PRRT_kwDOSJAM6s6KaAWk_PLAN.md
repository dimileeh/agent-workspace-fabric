# PRRT_kwDOSJAM6s6KaAWk pre-push dirty finalize diff-path normalization plan

## Problem statement
Review thread `PRRT_kwDOSJAM6s6KaAWk` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:614`) reports that
`_operation_owned_delta_paths` builds the operation-owned path set from raw
`git diff --name-only` output, while the dirty check
(`check_validation_worktree_clean` -> `changed_paths_from_porcelain`) parses
`git status --porcelain`. The two path representations differ in two ways:

1. **Renames.** `git status --porcelain` reports a rename as
   `R  oldname.txt -> newname.txt`, and `changed_paths_from_porcelain` yields
   *both* `oldname.txt` and `newname.txt`. `git diff --name-only` yields only
   the destination (`newname.txt`). When a repair leaves a staged rename dirty
   (e.g. `git add -A` succeeded but `git commit` failed), the dirty set
   includes both names but the owned set only has the destination, so
   `unrelated_dirty = dirty_paths - owned_delta_paths` treats the rename
   source as unrelated dirt and the finalize fails as
   `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` instead of finalizing the
   operation's own rename.
2. **C-quoting.** With `core.quotepath=true` (the git default), non-ASCII
   paths are emitted C-quoted by both `git status --porcelain` and
   `git diff --name-only`. `changed_paths_from_porcelain` already unquotes
   porcelain via `unquote_porcelain_path`, so the dirty set is decoded. The
   raw `git diff --name-only` lines parsed by
   `_operation_owned_delta_paths` are *not* unquoted, so a non-ASCII dirty
   path would never match its C-quoted `--name-only` form and would be
   treated as unrelated dirt (same fail-closed false positive).

`git diff --name-status -z` emits NUL-delimited records with both rename
source and destination, and never C-quotes paths (the `-z` form always uses
raw bytes), so parsing that output gives the same path representation the
dirty check uses. The repo already has a shared parser for it:
`changed_paths_from_name_status_z` in `awf.control.protected_file_diffs`
(re-exported as `_changed_paths_from_name_status_z` in
`awf.runtime.pr_monitor_runner.path_parsing`).

## Scope
- Change `_operation_owned_delta_paths`
  (`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`) to collect
  both the committed delta (`git diff --name-status -z
  operation_start_head..HEAD`) and the staged delta (`git diff
  --name-status -z --cached operation_start_head`) via
  `_changed_paths_from_name_status_z`, instead of `git diff --name-only`
  with raw line parsing.
- Preserve the existing fail-closed contract: if either git command fails
  *or* its parsed output is malformed (the parser raises
  `ProtectedScopeDiffError`), the helper returns `None` so the caller keeps
  the dirty fail-closed path instead of committing unowned dirt.
- Update the docstring of `_operation_owned_delta_paths` (and the
  `_try_finalize_pre_push_dirty_repair_state` docstring that references the
  diff commands) to describe `--name-status -z` and the rename/non-ASCII
  normalization rationale.
- No new abstractions, no caller signature changes, no unrelated refactor,
  no protected-file edits.

## Requirements checklist
- [ ] Add a regression test (TDD red first): a staged rename whose source
      path is dirty (porcelain yields both `oldname.txt` and `newname.txt`)
      is finalized instead of failing as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`, because the owned set now
      includes both names from `--name-status -z`.
- [ ] Add a regression test (TDD red first): a non-ASCII dirty path
      (`café.txt`, C-quoted by `git status --porcelain` and decoded by the
      porcelain parser) is finalized instead of failing as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`, because the owned set is
      parsed from `-z` (raw bytes, no C-quoting).
- [ ] Keep the existing unrelated-dirt fail-closed test green: a dirty
      path outside both the committed and staged operation deltas still
      fails as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
- [ ] Keep the existing delta-unavailable fail-closed test green: when
      either `git diff` fails, the helper returns `None` and the finalize
      is skipped (stays `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`).
- [ ] Keep the existing unowned-post-commit-delta test green: the post-commit
      re-validation still fails closed with
      `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA` when the committed delta gains
      an extra unowned path.
- [ ] Implement the minimal fix in `_operation_owned_delta_paths`: switch
      both diff invocations to `--name-status -z` and parse with
      `_changed_paths_from_name_status_z`.
- [ ] Confirm new + existing finalize tests pass (TDD green).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Add the rename-source regression test: queue a porcelain dirty check
   carrying both `oldname.txt` and `newname.txt`, queue
   `--name-status -z` committed+staged deltas that include the rename
   record, assert the finalize commits and validation proceeds. Confirm
   it fails on the unfixed code (the owned set lacks `oldname.txt`).
2. Add the non-ASCII path regression test: queue a porcelain dirty check
   carrying the decoded `café.txt`, queue `--name-status -z` deltas with
   the raw UTF-8 path, assert the finalize commits. Confirm it fails on
   the unfixed code (the owned set is C-quoted and never matches).
3. Extend the existing unrelated-dirt and delta-unavailable tests to
   supply `--name-status -z`-shaped delta output so they keep matching the
   new parser (they currently feed `--name-only` lines, which the new
   parser rejects as malformed -> delta unavailable -> fail-closed, which
   happens to still pass but for the wrong reason; update them so they
   exercise the intended path).
4. Update `_operation_owned_delta_paths` to run
   `git diff --name-status -z operation_start_head..HEAD` and
   `git diff --name-status -z --cached operation_start_head`, parse each
   with `_changed_paths_from_name_status_z`, and return `None` on git
   failure *or* `ProtectedScopeDiffError`.
5. Update the docstrings in `_operation_owned_delta_paths` and
   `_try_finalize_pre_push_dirty_repair_state` to reference
   `--name-status -z` and the rename/non-ASCII normalization rationale
   (review thread `PRRT_kwDOSJAM6s6KaAWk`).
6. Re-run the new + existing finalize tests (TDD green).
7. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q`
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/control/test_protected_file_diffs.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria
- New rename-source and non-ASCII regression tests fail on the unfixed
  code and pass on the fixed code.
- Existing finalize tests (finalize, no-op recheck, unrelated-dirt
  fail-closed, no-anchor fail-closed, delta-unavailable fail-closed,
  policy/ownership/protected-scope/provider-retry reason-code preservation,
  unowned-post-commit-delta fail-closed) still pass.
- Lint/typecheck clean on touched files.
