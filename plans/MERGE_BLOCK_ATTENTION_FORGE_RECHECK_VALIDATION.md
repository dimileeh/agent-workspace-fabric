# Validation: Forge Re-check for Merge-block Attention (#671)

Plan reference: `plans/MERGE_BLOCK_ATTENTION_FORGE_RECHECK_PLAN.md`

## Requirement Status

- Complete: Queue/reviewer/initial-grace waits decide `merge_block_attention` from forge mergeability status instead of marker age.
- Complete: Forge `BLOCKED` / `HAS_HOOKS` preserves the marker and stable `awaiting_human_since` without queue-wait re-stamping.
- Complete: Forge `CLEAN` clears the marker and `awaiting_human_since` promptly while still waiting on the non-human gate.
- Complete: Indeterminate or failed targeted re-check preserves conservatively.
- Complete: The #666 `allow_age_out=False` queue preserve/re-stamp branch was removed; `_clear_stale_merge_attention` remains scoped to the #661 merge critical-section TTL path.
- Complete: #661 critical-section behavior remains covered by focused merge-attention tests.

## Files Changed

- `src/awf/runtime/pr_monitor.py`
- `src/awf/runtime/pr_monitor_runner/gates.py`
- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `src/awf/runtime/pr_monitor_runner/mixins.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Passed: `40 passed in 63.34s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/gates.py src/awf/runtime/pr_monitor_runner/mixins.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/gates.py src/awf/runtime/pr_monitor_runner/mixins.py`
  - Passed: `Success: no issues found in 5 source files`
- Attempt 1 documentation repair checks:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py`
    - Passed: `All checks passed!`
  - `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/merge_attention.py`
    - Passed: `1 file already formatted`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime -k 'merge_attention or merge_block or stale or queue' -q`
    - Passed: `202 passed, 2574 deselected in 192.12s`
  - `git diff --check`
    - Passed with no output.

## Attempt 1 Conformance Repair

- Documentation repair: updated
  `src/awf/runtime/pr_monitor_runner/merge_attention.py` so
  `_clear_stale_merge_attention` and its durable re-stamp helper no longer claim
  queue/reviewer/initial-grace waits use the TTL re-stamp-on-preserve path. The
  text now states that re-stamping is limited to the merge critical-section TTL
  path, and queue/reviewer/initial-grace waits are forge-signal-driven via
  `_clear_or_preserve_merge_attention_for_queue_wait`.

## Attempt 2 Conformance Clarification

- The previous validation note incorrectly framed unavailable full-coverage
  evidence as a remaining plan-conformance gap. The saved plan's focused
  validation section explicitly excludes full coverage gates, whole-repository
  pytest, full `.awf/workspace.yml` validation, and frontend builds from the
  agent phase.
- The plan's validation requirement is the focused runtime evidence listed
  above plus the AWF-managed post-agent validation pass. The configured
  repository coverage gate remains a downstream AWF/GitHub merge gate outside
  this plan-conformance artifact; it is not a missing implementation or
  documentation requirement for this agent pass.
- No broad coverage command was run during this repair, consistent with the
  workspace contract and the saved plan non-goals.

## Attempt 3 Conformance Repair

- Retargeted the stale queue-wait TTL re-stamp regressions in
  `tests/unit/runtime/test_merge_queue_ordering.py`:
  - `test_clear_stale_merge_attention_restamps_preserved_marker` is now
    `test_merge_critical_section_restamps_preserved_marker`.
  - `test_clear_stale_merge_attention_restamps_preserved_marker_durably` is now
    `test_merge_critical_section_restamps_preserved_marker_durably`.
- Both tests now document the #661 merge critical-section-only TTL/re-stamp
  behavior. Queue/reviewer/grace waits remain covered by #671 forge-signal
  tests and preserve without re-stamping.
- Updated
  `test_merge_queue_wait_preserves_active_branch_protection_attention` to assert
  the queue-wait marker is stable when forge status is `BLOCKED`, rather than
  asserting or describing a queue-wait re-stamp.

Focused evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_merge_queue_wait_preserves_active_branch_protection_attention tests/unit/runtime/test_merge_queue_ordering.py::test_merge_critical_section_restamps_preserved_marker tests/unit/runtime/test_merge_queue_ordering.py::test_merge_critical_section_restamps_preserved_marker_durably -q`
  - Passed: `3 passed in 4.82s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_merge_queue_ordering.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/runtime/test_merge_queue_ordering.py`
  - Passed: `1 file already formatted`

## Attempt 4 Validation-Failure Repair

- Fixed the queue-wait verdict classifier so GitHub `CLEAN` is treated as a
  resolved merge-block signal and clears `merge_block_attention` plus
  `awaiting_human_since`.
- Preserved the documented Bitbucket exception: Bitbucket open PRs map to
  `CLEAN`, so Bitbucket `CLEAN` remains indeterminate and preserves
  conservatively.
- Retargeted the stale
  `test_clean_status_preserves_merge_block_attention_during_queue_wait`
  expectation to the Bitbucket-specific conservative path.

Focused evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_reviewer_settle_wait_when_forge_resolved -q`
  - Passed: `2 passed in 4.49s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime -k 'merge_attention or merge_block or stale or queue' -q`
  - Passed: `205 passed, 2574 deselected in 195.85s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed: `2 files already formatted`

Validation complete: Attempt 4 is the final validated state for this plan, and
its focused evidence confirms the plan-conformance requirements are satisfied.
The earlier attempts remain as repair history only; full AWF/GitHub validation
continues to run after the agent phase.
