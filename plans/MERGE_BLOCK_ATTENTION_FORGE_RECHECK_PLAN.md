# Implementation Plan: Forge Re-check for Merge-block Attention (#671)

## Problem Statement

`merge_block_attention` currently uses marker age and the #666 preserve-while-queued re-stamp to decide whether `awaiting_human_since` should remain surfaced while the PR monitor is parked behind non-human waits such as merge queue, non-check reviewer settle, or initial review grace. That creates both issue #671 error modes:

- false negative: an active branch-protection block can age past the TTL and be cleared while still blocked;
- false positive: a resolved block can be re-stamped indefinitely while queued, keeping `awaiting_human_since` visible after the human gate resolved.

The implementation will replace queue-wait age/re-stamp decisions with an observable forge branch-protection / mergeability re-check. The marker will be preserved only when the forge still says branch protection is active, cleared when the forge confirms resolution, and preserved conservatively when the re-check fails or is indeterminate.

## Intended Files and Modules to Touch

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
  - Replace the queue-wait calls that currently use `_clear_stale_merge_attention(..., allow_age_out=False)` for merge-queue, reviewer-settle, and initial-grace waits with a forge-signal-driven clear/preserve path.
  - Include both pre-lock wait arms and post-lock recheck arms.
  - Reuse the already-fetched `PRStatus` for the current poll where it is sufficient; for post-lock paths that already re-fetch `merge_status`, use that refreshed status.
  - Add a targeted status refresh only if the existing status is not sufficient to classify the marker state.

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
  - Add or adapt a helper that applies a queue-wait branch-protection verdict to `merge_block_attention` atomically/durably:
    - `still_active` -> preserve the existing marker and `awaiting_human_since` without re-stamping;
    - `resolved` -> clear the marker and `awaiting_human_since` in the existing atomic clear style;
    - `indeterminate/error` -> preserve marker and `awaiting_human_since` without re-stamping.
  - Remove the queue-wait TTL age-out/re-stamp behavior from the preserve path. Keep non-queue #661 merge-critical-section clears intact unless implementation proves they can share a helper without changing behavior.

- `src/awf/runtime/pr_monitor.py`
  - Update comments/docstrings around `MonitorState.merge_block_attention_active()` and `mark_merge_block_attention()` if they still describe queue-wait TTL as the contract.
  - Avoid changing the state key shape unless strictly necessary; the marker can remain a timestamp from actual merge rejection stamps, but queue-wait decisions must no longer depend on age.

- `tests/unit/runtime/test_merge_queue_ordering.py`
  - Re-target `test_clear_stale_merge_attention_restamps_preserved_marker` and related merge-queue wait coverage away from re-stamp assertions and toward forge-signal behavior.
  - Add explicit queue cases for forge-still-blocked, forge-resolved, and forge-indeterminate/error.

- `tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Retain #661 fast-path and merge-critical-section intent tests.
  - Update/remove queue-specific re-stamp expectations that conflict with the new no-re-stamp queue contract.
  - Keep durable clear/preserve regression intent around marker plus `awaiting_human_since` atomicity.

- Possibly `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py` and `tests/unit/runtime/test_pr_monitor_initial_review_grace.py`
  - Only if existing reviewer-settle or initial-grace tests need direct retargeting beyond the shared `_execute` tests in `test_merge_queue_ordering.py`.

## Tests to Write First

1. Forge says still blocked behind merge queue -> preserve stable attention
   - Seed a workspace with `awaiting_human_since` and a `merge_block_attention` marker from a prior merge rejection.
   - Execute `Merge()` into a merge-queue wait using a `PRStatus` whose forge signal indicates branch protection remains active, likely `merge_state_status in {BLOCKED, HAS_HOOKS}` with no higher-priority blocker.
   - Assert `awaiting_human_since` remains exactly the original timestamp, `awaiting_human_reason` remains present, and the marker is not re-stamped by the queue wait.
   - Assert no merge command/merge attempt occurred while queued.

2. Forge says resolved behind merge queue -> clear promptly
   - Seed the same attention and marker.
   - Execute into a merge-queue wait using a clean/mergeable `PRStatus` indicating branch protection is no longer active.
   - Assert the monitor still waits on the queue, but `awaiting_human_since`, `awaiting_human_reason`, and `merge_block_attention` are cleared promptly.
   - Assert the marker is not preserved or re-stamped.

3. Forge re-check is indeterminate or errors -> preserve conservatively
   - Use a status with insufficient/unknown mergeability signal or monkeypatch the targeted re-check/fetch to raise a forge error, depending on the final helper shape.
   - Assert the marker and original `awaiting_human_since` are preserved and stable.
   - Assert no re-stamp occurs on preserve.

4. Reviewer-settle and initial-grace parity
   - Retarget existing resolved-marker-preserved tests so they assert:
     - `merge_state_status=BLOCKED/HAS_HOOKS` preserves with a stable timer;
     - `merge_state_status=CLEAN` clears promptly.
   - Use the existing `_execute`-level reviewer-settle and initial-grace scenarios where possible to avoid duplicating setup.

5. #661/#663 intent preservation
   - Keep tests proving a resolved ordinary `NotifyHuman` attention clears before pre-merge settle / fast merge path.
   - Keep tests proving an active branch-protection escalation stays surfaced with a stable timer, now because the forge confirms blocked rather than because a TTL is fresh.
   - Keep durable atomic clear tests for marker + attention so a restart cannot observe one side without the other.

## Implementation Steps

1. Add failing tests first for merge-queue forge-blocked, forge-resolved, and forge-error/indeterminate behavior.
2. Retarget the old re-stamp tests to assert the same protective intent with the new forge signal and stable marker semantics.
3. Implement a small classification helper for queue-wait attention decisions, using `PRStatus.merge_state_status` as the primary observable signal:
   - `BLOCKED` / `HAS_HOOKS` -> still active;
   - clean mergeable states with no branch-protection signal -> resolved;
   - `UNKNOWN` or failed targeted status fetch -> indeterminate.
   If existing `PRStatus` fields prove insufficient while implementing, add one targeted forge status refresh through the existing `_fetch_status_for_decision` path and preserve on any `ForgeClientError`, `BaseFetchError`, or `BaseBehindCountError` from that refresh.
4. Wire the queue-wait paths in `merge_loop.py` to call the new forge-signal helper before sleeping/waiting:
   - pre-lock merge queue;
   - pre-lock reviewer-settle;
   - post-lock initial grace;
   - post-lock reviewer-settle;
   - post-lock merge queue.
5. Remove the #666 queue preserve re-stamp behavior. Queue-wait preserve must keep the existing marker and timer stable rather than writing a fresh timestamp.
6. Preserve the existing non-queue #661 behavior for merge-critical-section entry unless a test explicitly requires a compatible adjustment.
7. Update comments/docstrings so they describe the forge-signal contract, not TTL/re-stamp queue heuristics.
8. Create the implementation validation artifact required by `plans/PLAN_EXECUTION_PROTOCOL.md` during the implementation phase, recording files changed and focused command evidence.
9. Commit locally with a scoped message after implementation, per the workspace contract. Do not push, rebase, switch branches, or merge.

## Focused Validation Commands

Run only targeted checks during implementation; AWF/GitHub own broad validation after the agent exits.

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
- If touched: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py tests/unit/runtime/test_pr_monitor_initial_review_grace.py -q`
- Focused lint/type checks for touched runtime files only, if practical:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py`

Do not run full coverage gates, whole-repo pytest, full `.awf/workspace.yml` validation, or frontend builds during the agent phase unless the operator explicitly requests that diagnostic.

## Risks and Assumptions

- Assumption: `PRStatus.merge_state_status` is an adequate observable branch-protection signal for the queue decision when it is `BLOCKED` or `HAS_HOOKS`, and clean mergeability states are adequate to confirm resolution. If status is `UNKNOWN` or otherwise ambiguous, preserve.
- Assumption: existing status fetches in the monitor poll and post-lock recheck paths are acceptable forge observations; a targeted additional fetch is only needed for ambiguous current status.
- Risk: `merge_state_status=BLOCKED/HAS_HOOKS` can also represent transient required-check absence. Existing #660/#662 required-check grace behavior must remain unchanged; tests should avoid classifying absent-check grace as a resolved human block.
- Risk: clearing marker and attention must remain atomic/durable to avoid restart windows. Reuse existing `get_for_update` transaction patterns.
- Risk: removing re-stamp assertions may reduce coverage on timestamp branches. Replace them with behavioral tests over blocked/resolved/error branches, not coverage padding.
- Risk: Bitbucket may not expose GitHub-style branch-protection semantics. Treat unsupported or indeterminate forge signals as preserve to avoid false-clearing escalation.

## Explicit Non-goals

- Avoid changing branch management, push behavior, PR creation, or merge behavior.
- Leave #660/#662 required-check grace logic and broader `decide()` gate ordering unchanged.
- Preserve #661 ordinary resolved-human attention clears before merge/pre-merge settle.
- Do not introduce new branch-protection policy abstractions beyond what is required for this queue-wait marker decision.
- Keep broad AWF/GitHub-owned validation in the post-agent phase unless the operator explicitly requests it.
- Manual review-comment resolution and PR merging remain out of scope for the agent phase.
