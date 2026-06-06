# Plan — #431 + #432: plan-only gate follow-ups to #427

Workspace: `ws_b9e1bbf6f9d74d0da6e492de`
References (must appear in PR body): **#431**, **#432** (both deferred from #427).

## Goal

Two related, scoped cleanups, both in the executor plan-only gates. One PR.

- **PART 1 (#431)** — DRY: replace the inline "staged plan-only AND committed
  `base..HEAD` plan-only" computation at the post-agent / running-phase gate with the
  shared helper `_committed_and_staged_output_is_plan_only(...)` introduced by #427.
  **Behavior-preserving.**
- **PART 2 (#432)** — Correctness: make the final pre-push plan-only gate
  authoritative by dropping the `not has_known_non_plan_output and` guard, then
  **remove the now-dead sticky `has_known_non_plan_output` flag entirely** (verified to
  have exactly one decision-reader).

## Verification of the #432 precondition (DONE during planning)

The flag `has_known_non_plan_output` was grepped across `src/`. Every occurrence is
init / assignment / kwarg-threading / dataclass-field / result-construction. The **only**
site that reads it in a branch condition is:

```text
src/awf/control/executor/execution_flow.py:1155
    if not has_known_non_plan_output and await self._fail_if_plan_only_committed_output(...)
```

A targeted grep for conditional reads (`if … has_known_non_plan_output`,
`not has_known_non_plan_output`, `return has_known_non_plan_output`, boolean operators)
across `src/**/*.py` returned **only line 1155**. Therefore removing the flag is safe and
PART 2b proceeds. (If implementation re-grep ever finds a second decision-reader, STOP:
ship only 2a and note the blocker in the PR.)

> Note: line numbers in the task text (680–698, 1135, ~630) are approximate; current
> file positions are post-agent gate `execution_flow.py:700–719`, init `:650`, final gate
> `:1155`, validation call `:1001–1018`, read-back `:1026`.

## Intended files to touch

### Production code
- `src/awf/control/executor/execution_flow.py`
  - **PART 1**: replace the inline block (lines ~700–718) with a single
    `await self._committed_and_staged_output_is_plan_only(worktree_path=..., base_commit=base_commit, staged_paths=staged_paths)` followed by the existing
    `await self._fail_if_plan_only_paths(workspace_id=..., changed_paths=staged_paths, expected_status=WorkspaceStatus.running)` guard. Keep the protected-file diff
    checks (lines 720–739) unchanged.
  - **PART 1 cleanup**: remove the now-unused import
    `changed_paths_are_only_internal_plan_artifacts` (line 84) — after PART 1 it is no
    longer referenced in this file (confirmed: only used at the inline block). Leave
    `self._committed_paths_since` (a method, not an import) alone.
  - **PART 2a**: drop the `not has_known_non_plan_output and` prefix at line 1155 so
    `_fail_if_plan_only_committed_output(...)` always runs.
  - **PART 2b**: delete `has_known_non_plan_output = False` (line 650), the
    `has_known_non_plan_output = True` assignment in the PART 1 block (line 719), the
    `has_known_non_plan_output=has_known_non_plan_output` kwarg on the
    `run_validation_and_fix_cycle(...)` call (line 1016), and the read-back assignment
    `has_known_non_plan_output = validation_result.has_known_non_plan_output` (line 1026).
- `src/awf/control/executor/execution_validation.py`
  - Remove the `has_known_non_plan_output: bool` parameter from
    `run_validation_and_fix_cycle` (line 164).
  - Remove the `has_known_non_plan_output = True` assignment (line 1211).
  - Remove every `has_known_non_plan_output=has_known_non_plan_output` kwarg from each
    `ExecutionValidationResult(...)` construction and each `_fail_validation_worktree_guard(...)`
    call in this module (~30 sites; grep `has_known_non_plan_output` to enumerate).
- `src/awf/control/executor/validation_cleanup_guards.py`
  - Remove the `has_known_non_plan_output: bool` field from the
    `ExecutionValidationResult` dataclass (line 36).
  - Remove the `has_known_non_plan_output` parameter of `fail_validation_worktree_guard`
    (line 47) and its use in that function's result construction (line 76).
  - Remove the threading kwargs at lines 167, 179, 190, 239.

### Tests (see TDD section for new vs. mechanical)
- `tests/unit/control/test_executor_parts/test_executor_part_006.py`
  (final-gate behavior — PART 2 home).
- `tests/unit/control/test_executor_post_agent_commit_parts/...`
  (post-agent gate behavior — PART 1 home; add a new `_part_004.py` or extend
  `_part_003.py` following the existing module-splitting convention).
- Mechanical field/param removal in fixtures/assertions (NOT new tests):
  - `tests/unit/control/test_executor_runtime_profile_snapshot.py:640`
  - `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py` (139, 278, 392, 525, 531)
  - `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py` (661, 666, 718, 723, 793, 900, 1011, 1017)
  - `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py` (`_run_cycle` kwarg at 188; assertion at 861) — see tension note below.
  - `tests/unit/control/test_executor_parts/test_executor_part_006.py:626` (comment text referencing the flag).

## Behavior-equivalence argument for PART 1

Original inline logic only calls `_fail_if_plan_only_paths` when
`staged_paths_are_plan_only AND committed_output_is_plan_only`. The helper
`_committed_and_staged_output_is_plan_only` (quality_methods.py:304) returns exactly
`changed_paths_are_only_internal_plan_artifacts(staged_paths) AND (no committed_paths OR
changed_paths_are_only_internal_plan_artifacts(committed_paths))` — i.e. the identical
conjunction, computing `committed_paths` via the same `self._committed_paths_since`. The
subsequent `_fail_if_plan_only_paths(changed_paths=staged_paths, ...)` re-checks
plan-only on the same `staged_paths` (always True here) and marks failed identically.
Net: same calls, same short-circuit, same `WorkspaceStatus.running`, same message. The
mirror is already proven by the #427 site in execution_validation.py:1183–1192, which
this rewrite makes structurally identical.

## Tests to write FIRST (strict TDD)

### PART 1 — post-agent gate (in test_executor_post_agent_commit_parts/, NOT part_009)
1. **Genuine plan-only post-agent output still fails** (regression-guard, must stay/turn
   green): drive `execute()` where post-agent staged paths are only
   `docs/awf-plans/<ws>.md`-style artifacts and net `base..HEAD` is empty/plan-only →
   workspace fails `PLAN_ONLY_OUTPUT` at `WorkspaceStatus.running`, no push / no PR.
2. **Committed real output passes the post-agent gate**: staged plan-only artifact but
   real committed work in `base..HEAD` → gate does NOT fail; execution proceeds past the
   post-agent block (assert `_fail_if_plan_only_paths` not awaited / no PLAN_ONLY at
   running). Spy that `_committed_and_staged_output_is_plan_only` is the routed seam
   (assert it is awaited), proving the helper adoption.
   - Existing post-agent plan-only tests in `test_executor_post_agent_commit_part_003.py`
     (`..._plan_only_change_is_blocked`, `..._normalizer_only_plan_output_is_blocked`)
     must remain green unchanged (behavior-preserving check).

### PART 2 — final gate authoritative + revert false-negative (in test_executor_part_006.py)
3. **CRITICAL revert regression** (red against old code): drive `execute()` so an early
   step produces real staged/committed output (old code would set the sticky flag True),
   then the net `base..HEAD` diff is empty/plan-only at the final gate. Stub
   `_fail_if_plan_only_committed_output` → True (net is plan-only). Assert the final gate
   runs and `execute()` returns before push (`no "push"`, no `gh pr create`). Under the
   OLD flag-guarded gate this would be skipped and a PR would open — so this test fails
   pre-change and passes post-change.
4. **Normal-path (real committed output opens PR)**: net `base..HEAD` has real output →
   `_fail_if_plan_only_committed_output` returns False → push + `gh pr create` proceed.
   Update existing `test_plan_only_committed_output_returns_before_push` (line 612): its
   docstring/comment about "`has_known_non_plan_output` stays False" must be rewritten to
   "the final gate is now always evaluated"; the queued-command shape stays valid.

### Mechanical test maintenance (field/param removal — NOT new coverage)
5. Remove `has_known_non_plan_output=...` kwargs from every `ExecutionValidationResult(...)`
   constructed in tests and from the part_009 `_run_cycle` helper; remove
   `assert result.has_known_non_plan_output is ...` assertions (they assert a deleted
   field). Where a removed assertion was the test's only point, fold its intent into the
   surviving behavioral assertions rather than deleting the test.

> **Tension note for reviewer**: the task says "do NOT add to
> test_executor_coverage_edges_part_009.py". I will NOT add new test functions there, but
> removing the now-deleted `has_known_non_plan_output` parameter from its shared `_run_cycle`
> helper (line 188) and the dead field assertion (line 861) is mechanically required for the
> module to import/run after PART 2b. These are deletions, not additions. Called out
> explicitly so it is not mistaken for scope creep.

## Validation commands (run locally — focused; full gate is AWF/CI-owned)

```bash
# Narrow, fast iteration on the touched suites
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_006.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_post_agent_commit_parts -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q

# Focused lint/type on touched modules
uv run --python 3.12 --extra dev ruff check src/awf/control/executor tests/unit/control
uv run --python 3.12 --extra dev ruff format --check src/awf/control/executor
uv run --python 3.12 --extra dev mypy
```

AWF/GitHub CI owns the full validation suite, the aggregate 99% coverage gate, and merge
gating after the agent phase — not run here.

## Risks & mitigations

- **PART 1 not behavior-preserving** → mitigate by keeping pre-existing post-agent
  plan-only tests untouched and green, plus the equivalence argument above.
- **Hidden second decision-reader of the flag** → precondition grep found only line 1155;
  re-grep during implementation. If a second reader appears, ship only PART 2a and note
  the blocker (do NOT remove the flag).
- **mypy/ruff fallout from signature/dataclass change** → frozen dataclass field removal
  and parameter removal ripple through ~30 call sites in `execution_validation.py` and the
  guards/tests; enumerate via grep and remove all in one pass; `mypy` (no path args) and
  `ruff check` catch stragglers (e.g. the now-unused import at line 84).
- **Coverage dip** → files shrink (dead flag removed); the new PART 1/PART 2 tests cover
  the rerouted branches. No coverage exclusions anticipated.
- **State-machine / commit boundary** → no transition or commit-boundary changes; gates
  use existing `_mark_failed` and `WorkspaceStatus.{running,validating}` exactly as before.

## Assumptions

- `_committed_and_staged_output_is_plan_only` (quality_methods.py:304) and its mixin
  wiring (mixins.py:104) are already present from #427 — confirmed.
- The post-agent gate currently only triggers when `staged_paths` is non-empty; PART 1
  preserves that outer `if staged_paths:` guard.
- `_fail_if_plan_only_committed_output` already evaluates the correct net `base..HEAD`
  output (quality_methods.py:325) and returns False for branches with real committed
  output — confirmed; making it unconditional cannot false-fail a real branch.

## Non-goals

- No change to merge gating, the PR monitor, scheduler, or any state transition.
- No refactor of the protected-file / supply-chain / quality-gate checks.
- No touching the #427 helper itself or the execution_validation.py:1183 site it already
  serves (other than removing the dead flag kwargs/result field).
- No new abstractions; reuse existing helpers and test conventions.
- No edits to `test_executor_coverage_edges_part_009.py` beyond the mechanical
  field/param deletions required for it to compile.
