# Plan: AWF-authored post-validation conformance report must not dirty worktree (#604)

## Problem

After a workspace passes AWF validation, `_run_post_validation_conformance_check`
runs one more planning-conformance pass. If the agent does not write a fresh
satisfied conformance report file, AWF synthesises one into
`docs/awf-plans/{workspace_id}.conformance.json` itself. That path is **tracked
by the repo** (or can be, in project-specific profiles) and is intended to be an
operator-visible artifact, but because it remains dirty on the worktree it can be
detected as a pre-existing dirty worktree by the PR monitor's pre-push
validation guard. The guard then fails the workspace with
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` even though the only "dirty" file is the
satisfied conformance report that AWF itself just authored.

Issue #544 already stopped *committing* that report (because the awf-self profile
gitignores `docs/awf-plans/*`). Issue #604 is the narrower follow-on: even when
the report is written, it must not be left dirty in a state that fails later
validation cleanliness checks.

## Scope

1. Fix the post-validation conformance path so a satisfied report written by
   AWF does not remain as a tracked dirty file at pre-push validation time.
2. Keep the PR monitor dirty-worktree guard strong: real source/config/test
   changes still fail if they are dirty.
3. Preserve behaviour for:
   - a fresh report produced by the agent (stdout or disk)
   - a real conformance failure (not satisfied)
   - an OSError while writing the report (non-fatal)
4. Add focused regression coverage for a tracked pre-existing conformance report
   path that would otherwise become dirty.

## Non-goals

- Do not globally whitelist `docs/awf-plans/**` in validation cleanliness checks.
- Do not change the PRD; the PRD is silent on this detail and the code has
  already made an explicit decision that the report is an AWF artifact, not
  user work.
- Do not refactor the planning loop, the validation worktree guard, or the PR
  monitor pre-push validation machinery.

## Root cause

`_run_post_validation_conformance_check` in
`src/awf/control/executor/planning_ops.py` calls
`_write_satisfied_post_validation_conformance_report` when the report is not
already fresh on disk. The function writes the JSON to the worktree path and
returns. Nothing then removes or restores the file, so it shows up in
`git status --porcelain` as an untracked/ignored or tracked modification.

For the awf-self profile the file is gitignored, so
`check_validation_worktree_clean(..., ignore_all_ignored=True)` ignores it.
But project-specific profiles may place the conformance report under a tracked
path, or a repo may not gitignore `docs/awf-plans/*`, and then the file becomes
a real dirty path that the PR monitor's pre-push validation will reject.

## Proposed implementation

### Design

Treat the AWF-synthesised satisfied post-validation conformance report as
runtime artifact/event state, not as source work. After it has been written, AWF
must clean it from the worktree before returning from the conformance check so
that the downstream validation and push steps see a clean tree.

The cleanest way to do this without weakening the dirty-worktree guard is:

1. After a successful satisfied report is produced (whether fresh from the agent
   or synthesised by AWF), ensure the file is removed from the worktree so it
   cannot appear as dirty later.
2. The report's outcome is already durably captured by
   `workspace.post_validation_conformance_satisfied` event and by the validation
   run artifact deposit mechanism, so removing the on-worktree copy does not
   lose information.
3. For the awf-self profile the path is gitignored, so `git clean` or a simple
   `path.unlink()` both work. Use `path.unlink(missing_ok=True)` after the
   event is recorded (or before returning success) because it is deterministic
   and does not require a git command.

### Why not stage/commit the file or globally whitelist it?

- Committing was rejected by #544 because the awf-self profile deliberately
  gitignores the path and `git add` failed.
- Globally whitelisting `docs/awf-plans/**` in `check_validation_worktree_clean`
  would let real agent mistakes (e.g. a tracked `docs/awf-plans/README.md` edit
  left dirty) slip through pre-push validation, weakening merge safety.
- Restoring the file from HEAD is not appropriate because the file is often
  intentionally untracked/ignored and may not exist in HEAD.

### Files/modules to touch

1. `src/awf/control/executor/planning_ops.py`
   - In `_run_post_validation_conformance_check`, after recording the
     `workspace.post_validation_conformance_satisfied` event (and after the
     best-effort write), remove the on-worktree report file if it exists.
   - Keep the write non-fatal.
   - Ensure the removal is also best-effort and non-fatal (do not fail the
     workspace if the file is read-only or absent).
   - Preserve the fresh-on-disk path: if the agent wrote a fresh report, still
     remove the on-worktree copy before returning success, because the file is
     an AWF artifact and leaving it dirty would cause the same problem.

2. `src/awf/control/executor/execution_validation.py`
   - After a successful post-validation conformance check, deposit the plan +
     conformance report into the served artifact dir BEFORE the function
     returns, because `_run_post_validation_conformance_check` will now remove
     the report from the worktree before it returns.
   - A single best-effort deposit call at the success branch is sufficient; it
     must happen before the worktree copy is removed.

3. `src/awf/control/executor/planning_artifacts.py`
   - No functional change required: `deposit_workspace_planning_artifacts`
     already copies from a worktree-relative source. The deposit must simply be
     moved or duplicated to occur before the report is removed.

### Implementation steps

1. Read `src/awf/control/executor/planning_ops.py` and confirm the exact
   insertion point for the file removal.
2. Update `_run_post_validation_conformance_check` to unlink the report path
   after a satisfied success, after the event is recorded. Use the same
   `report_path` local variable (computed as `worktree_path / handoff.report_path`).
3. Update `execution_validation.py` to call `_deposit_planning_artifacts_best_effort`
   on the success branch BEFORE returning so the console can still surface the
   conformance report.
4. Write the regression test first and confirm it fails without the fix.
5. Run the focused test suite:
   - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py tests/unit/control/test_planning_ops_branch_edges.py -q`
6. Run lint/type checks on the touched files.
7. Write `plans/WS_D7E7539D5D2E4DB8BFEED3A5_VALIDATION.md` and stop.

## Risks and assumptions

- Risk: Removing the report after writing it may break console artifact
  visibility if the deposit step expects to copy from the worktree later.
  Mitigation: deposit the conformance report into the served artifact dir
  immediately during post-validation conformance success, before it is removed
  from the worktree.
- Risk: A profile that commits the conformance report (non-gitignored) will lose
  the on-worktree copy. Mitigation: that is exactly the desired behaviour; the
  report is AWF state and should not be committed. The event + artifact deposit
  already capture it.
- Assumption: The file is written only when the report is satisfied. Unsatisfied
  reports are intentionally left for diagnosis, and the conformance failure path
  is unchanged.
- Assumption: `path.unlink(missing_ok=True)` is safe for gitignored paths and
  tracked paths alike; it only removes the worktree file and does not touch the
  index.

## Validation commands

- Targeted tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py tests/unit/control/test_planning_ops_branch_edges.py -q`
- Lint/type:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/executor/execution_validation.py`
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/executor/execution_validation.py`

## Explicit non-goals

- No changes to `.gitignore`.
- No changes to `INTERNAL_PLAN_ARTIFACT_PREFIX` or `changed_paths_are_only_internal_plan_artifacts`.
- No changes to the PR monitor dirty-worktree guard.
- No full test suite or coverage gate run inside the agent phase.
