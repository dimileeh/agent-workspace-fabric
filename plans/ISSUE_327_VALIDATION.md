# Issue #327 — Validation (Iteration 1)

## Gap review

The prompt identified three remaining conformance gaps against the plan
(`docs/awf-plans/ws_4323a483bacd45b9b92b81bb.md`):

| # | Gap | Status |
|---|-----|--------|
| 1 | Missing regression test: nothing-to-commit + orphan-history → branch gets reattached (plan test #4) | **Fixed** |
| 2 | `plans/ISSUE_327_VALIDATION.md` not written | **Fixed** (this document) |
| 3 | `_is_nothing_to_commit` uses single pattern `r"nothing to commit"` instead of planned three-pattern regex | **Fixed** |

## Changes made

### 1. Expanded `_is_nothing_to_commit` regex

**File**: `src/awf/control/executor/quality_gates.py`

Changed from:
```python
_NOTHING_TO_COMMIT_PATTERN = re.compile(r"nothing to commit", re.IGNORECASE)
```
To:
```python
_NOTHING_TO_COMMIT_PATTERN = re.compile(
    r"nothing to commit|working tree clean|no changes added",
    re.IGNORECASE,
)
```

This matches the plan's specification exactly. While git reliably emits
"nothing to commit" in this scenario, the broader patterns cover edge-case
git versions and alternative output formats as the plan intended.

**Test update** (`test_executor_post_agent_commit_classifier.py`): Added
parametrized test cases for "working tree clean" and "no changes added"
to the `test_is_nothing_to_commit_detects_benign_clean_tree` test.

### 2. Added orphan-history regression test

**File**: `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_002.py`

Added `test_nothing_to_commit_orphan_history_is_reattached` to the
`TestSelfCommittedAgent` class. This test simulates:

- Agent self-commits (git commit returns "nothing to commit")
- Branch is ahead of base (rev-list count > 0)
- merge-base --is-ancestor fails (orphan history detected)
- `git reset --soft <base>` succeeds (orphan recovery)
- Fresh commit succeeds
- Post-recovery ancestor check passes
- Workspace proceeds to completion (not failed)

This covers plan test #4 — the combination of `_is_nothing_to_commit`
fall-through with the orphan-history guard path. Existing orphan tests in
`test_executor_part_005.py` only covered the normal-commit path
(`commit_result.ok=true`), so this was genuinely untested.

### 3. Validation document

This document (`plans/ISSUE_327_VALIDATION.md`) is the third gap — now
resolved.

## Acceptance criteria verification

| Criterion | Evidence |
|-----------|----------|
| Agent self-commits, branch ahead → proceeds | Existing test `test_nothing_to_commit_branch_ahead_proceeds` passes |
| Clean tree, branch NOT ahead → `agent_failure` | Existing tests `test_nothing_to_commit_not_ahead_fails_as_agent_failure` + `test_nothing_to_commit_empty_staged_not_ahead_fails_as_agent_failure` pass |
| Real git commit error → `POST_AGENT_COMMIT_FAILED` | Existing test `test_nonzero_git_commit_raises_and_marks_failed` passes |
| Orphan history guard still runs for self-committed branches | **New** `test_nothing_to_commit_orphan_history_is_reattached` passes |
| `_is_nothing_to_commit` detects broader patterns | Classifier unit tests pass with new parametrized cases |
| No file exceeds 1500-line guard | No file sizes changed materially |

## Focused test results

```
tests/unit/control/test_executor_post_agent_commit_classifier.py::test_is_nothing_to_commit_detects_benign_clean_tree  PASSED (13 cases)
tests/unit/control/test_executor_post_agent_commit_classifier.py::test_is_nothing_to_commit_rejects_real_errors  PASSED (4 cases)
tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_002.py::TestSelfCommittedAgent  PASSED (4 tests)
tests/unit/control/test_executor_parts/test_executor_part_005.py -k orphan  PASSED (2 tests)
```

Lint and type checks:
- `ruff check` — all checks passed
- `mypy src/awf/control/executor/quality_gates.py` — success, no issues

## Full AWF/GitHub validation

Full validation (wide `ruff check`, `mypy`, `pytest --cov`, frontend
builds, openapi drift check) is managed by AWF after agent completion, as
required by the workspace contract. Only focused checks were run inside
the agent phase.

## Deviations from plan

None. All three iteration-1 gaps are closed exactly as specified.
