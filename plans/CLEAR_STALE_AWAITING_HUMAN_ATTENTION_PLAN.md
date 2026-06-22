# Plan: Clear stale awaiting-human attention during merge and non-human gate waits (#661, #663)

This is the agent's execution-contract plan, derived from the saved workspace
plan at `docs/awf-plans/ws_c0848ad8288f4579852c60a1.md`. Execute against this
plan and validate in `plans/CLEAR_STALE_AWAITING_HUMAN_ATTENTION_VALIDATION.md`.

## Problem (shared root)

`awaiting_human_since` (the surfaced "awaiting human" attention flag) stays set
while the monitor is NOT actually waiting on a human, in two windows:

- **#661 (merge_loop.py)**: after a resolved `HUMAN_WAIT` episode, `decide()`
  returns `Merge`. `loop._execute` skips clearing attention for the `Merge` arm
  (so a still-blocked branch-protection fallback's COALESCE'd episode start is
  not reset each poll). The merge loop only clears stale attention before SOME
  non-human waits (merge queue, reviewer settle, initial review grace) — NOT
  before the pre-merge settle sleep and NOT on the fast path straight into the
  merge critical section (when `pre_merge_settle_seconds == 0`). So console
  KPIs/badges show "awaiting human" for ~90s while the monitor is actively
  merging.

- **#663 (lifecycle.py / merge_loop.py)**: when a branch-protection merge
  rejection has been FIXED externally, the next poll still loads the persisted
  `merge_block_attention` marker while `decide()` returns `Merge`. If that poll
  hits a merge-queue / reviewer-settle / initial-grace wait,
  `_clear_stale_merge_attention` skips the clear because the marker is still
  set — so CLI/console/KPI keep reporting a human wait even though only
  NON-human gates remain.

**Root**: clearing is blocked because (a) `_execute` skips clearing for `Merge`
and several early-return wait paths don't clear, and (b) the "resolved" case is
code-indistinguishable from the "still-blocked" case, and a naive clear would
reset the genuinely-blocked "awaiting human for N" timer.

## The shared fix (hold scope)

1. **DECOUPLE episode-start surfacing from DB-flag presence**: clearing the
   column during merge / non-human waits in the RESOLVED case is safe because
   the still-blocked case re-sets it every poll (the branch-protection fallback
   calls `_set_workspace_attention` + COALESCE each cycle), so a clear-then-reset
   does NOT age the timer. The clear decision depends on whether the block is
   STILL active — not on the DB flag being present.

2. **Distinguish RESOLVED vs STILL-BLOCKED via a bounded marker TTL on
   `merge_block_attention`**: the branch-protection fallback re-stamps the marker
   EVERY poll while blocked. A marker that has NOT been refreshed within a
   bounded TTL means no fallback has fired recently ⇒ the block has resolved.
   The TTL is bounded to a small multiple of `poll_interval_seconds` so a
   fresh marker (still-blocked) is preserved and a stale marker (resolved) is
   cleared.

3. **Apply the clear on the paths that currently skip it**:
   - #661: at critical-section entry (covers both the pre-merge settle sleep and
     the fast path into the merge attempt).
   - #663: the merge-queue / reviewer-settle / initial-grace early-return wait
     paths already call `_clear_stale_merge_attention`; the TTL makes that
     helper clear the RESOLVED case instead of unconditionally preserving it.

## Intended files / modules to touch

### `src/awf/runtime/pr_monitor.py`
- Replace the boolean `"1"` marker value with a stored ISO-8601 wall timestamp
  string. Add:
  - `mark_merge_block_attention(self, *, now: datetime | None = None)` — stamps
    `now` (default `datetime.now(UTC)`), stored as `.isoformat()`.
  - `merge_block_attention_active(self, *, now: datetime | None = None,
    ttl_seconds: float | None = None) -> bool` — returns True only when the
    marker is present AND (no TTL configured OR age <= TTL). When stale
    (age > TTL), treat as RESOLVED → return False (so the clear proceeds) and
    drop the marker.
  - Keep `clear_merge_block_attention` as-is (idempotent pop).
  - Add `merge_block_attention_ttl_seconds: float` to `MonitorConfig`
    (default `2 * poll_interval_seconds`, capped to a sane min/max window).
    Document that the fallback re-stamps every poll while blocked so the TTL
    only expires a RESOLVED block.
- Backward-compat: a legacy boolean `"1"` value is treated as
  "fresh/unknown-age" on first load (preserve, do not clear on age alone the
  first poll); the next fallback re-stamps it to a timestamp.

### `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- Update `_clear_stale_merge_attention` to pass `now` and the configured TTL to
  `state.merge_block_attention_active`, so a STALE (resolved) marker no longer
  blocks the clear. When the marker is stale, the helper clears it (via
  `state.clear_merge_block_attention()`) and proceeds with
  `_clear_workspace_attention`. When fresh (still-blocked), behavior is
  unchanged (preserve — regression #663 intact).

### `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- Branch-protection fallback (line ~1389): call
  `state.mark_merge_block_attention()` (now stamps `now`). No behavioral change
  for the still-blocked case.
- #661: Add a `_clear_stale_merge_attention(workspace_id, state)` call at the
  top of the `async with self._merge_coordinator.serialized_merge(...)` block,
  right after the `merge_critical_section_entered` event, BEFORE the
  `pre_merge_settle_seconds` branch. This single call covers both the
  settle-sleep path (clear before sleep) and the fast path (clear before merge
  attempt), and the existing merge-queue/settle/grace clears earlier in the
  function remain unchanged.
- The existing `_clear_stale_merge_attention` calls at merge-queue (532),
  reviewer-settle (565, 1049), and initial-grace (1013) stay; they now clear
  the RESOLVED case via the TTL.

### `src/awf/db/repositories/workspace_repo_attention.py`
- No change. `clear_workspace_attention` already guarded by
  `awaiting_human_since IS NOT NULL` (no-op when already clear).

## Tests to write first (TDD)

Add to `tests/unit/runtime/test_pr_monitor_merge_failures.py` and
`tests/unit/runtime/test_merge_queue_ordering.py` (co-located with the two
regressions they must coexist with):

### #661 — RESOLVED HUMAN_WAIT → Merge → attention cleared before merge
1. `test_resolved_human_wait_clears_attention_before_pre_merge_settle`:
   - Seed `awaiting_human_since` (resolved HUMAN_WAIT episode).
   - `MonitorState()` (no merge_block marker — resolved NotifyHuman, not
     branch-protection).
   - `pre_merge_settle_seconds > 0`.
   - Assert: terminal False, no merge attempted, AND
     `workspace.awaiting_human_since is None` (cleared before the settle
     sleep), `workspace.awaiting_human_reason is None`.
2. `test_resolved_human_wait_clears_attention_on_fast_path_into_merge`:
   - Same seed, `pre_merge_settle_seconds == 0`.
   - Assert merge SUCCEEDS and `workspace.awaiting_human_since is None`
     (cleared at critical-section entry before the merge attempt).

### #661 regression — still-blocked fallback keeps episode-start stable
- `test_merge_blocker_fallback_keeps_attention_since_stable_across_polls`
  (EXISTING — must stay green, unchanged). The fallback re-stamps the marker
  each poll, so the TTL never expires while blocked.

### #663 — branch-protection RESOLVED then merge-queue/settle/grace wait
3. `test_resolved_branch_protection_marker_cleared_on_merge_queue_wait`:
   - Seed `awaiting_human_since` AND a STALE `merge_block_attention` marker
     (timestamp older than the TTL) — simulating a block that resolved
     externally between polls.
   - `decide()` returns `Merge`; poll parks on merge-queue wait.
   - Assert: terminal False, parked on queue, AND
     `workspace.awaiting_human_since is None` (stale marker cleared), marker
     dropped from state.
4. (Parity) `test_resolved_branch_protection_marker_cleared_on_reviewer_settle_wait`
   and `test_resolved_branch_protection_marker_cleared_on_initial_grace_wait`
   with the same stale-marker seed, parked on the respective non-human gate.

### #663 regression — still-blocked through a merge-queue wait
- `test_merge_queue_wait_preserves_active_branch_protection_attention`
  (EXISTING — must stay green). The marker is FRESH (set on the same poll via
  `active_block_state.mark_merge_block_attention()`), so the TTL preserves it
  unchanged. Update the test to stamp the marker with `now` (the new signature)
  so it stays fresh within the TTL; assert `awaiting_human_since == episode_start`
  and the marker remains set.

### Marker TTL unit tests
5. `tests/unit/runtime/test_pr_monitor_state.py` (new file, co-located):
   - `mark_merge_block_attention` stamps a timestamp; `merge_block_attention_active`
     is True within TTL, False after TTL (and drops the marker when stale).
   - Legacy boolean `"1"` marker is preserved on first read (treated as
     fresh) and re-stamped on the next `mark_merge_block_attention`.

## Validation commands (focused — do NOT run the full suite/coverage gate)

AWF owns broad validation; run only targeted checks for the touched files:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_failures.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_state.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/merge_loop.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_failures.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_state.py -q
```

Full AWF/GitHub validation (full suite, `pytest --cov`, `--cov-fail-under=99`,
OpenAPI drift, console build) is managed by AWF after agent completion and is
NOT run inside the agent phase.

## Risks, assumptions, explicit non-goals

### Risks
- **TTL tuning**: too short a TTL could clear a still-blocked marker if a poll
  is delayed beyond the TTL. Mitigated by bounding the TTL to a multiple of
  `poll_interval_seconds` (default ~2×) and re-stamping every poll while
  blocked — a blocked poll's marker is always fresh.
- **Monitor restart with a stale marker**: a marker persisted from a resolved
  block that restarts after the TTL is correctly treated as resolved (cleared)
  on the first non-human-gate wait. This is the desired behavior (#663).
- **Legacy boolean marker**: treated as fresh on first load (preserve), then
  re-stamped to a timestamp by the next fallback. No regression for in-flight
  monitors.

### Assumptions
- The branch-protection fallback is the ONLY setter of `merge_block_attention`
  (verified: single call site at merge_loop.py:1389; the merge-method-preflight
  arm deliberately does NOT set it).
- `_clear_stale_merge_attention` is the single chokepoint guarding all
  non-human-gate clears; updating it + the new critical-section-entry clear
  covers all #661/#663 paths.
- `clear_workspace_attention`'s `IS NOT NULL` guard makes the added clears
  no-ops when the flag is already clear (no row churn).

### Explicit non-goals
- Do NOT change `decide()` gate logic or the #656/#660 grace work.
- Do NOT add a forge re-check / branch-protection API call per poll (TTL chosen
  instead to stay scoped and avoid extra API surface).
- Do NOT change `set_workspace_attention` COALESCE semantics or the
  `awaiting_human_since` column schema.
- Do NOT touch the merge-method-preflight arm (it records a sticky blocker
  and never reaches the `Merge`-arm non-human gate waits).
- Do NOT refactor unrelated code, split files, or introduce new abstractions.
- Do NOT run the full validation suite / coverage gate / OpenAPI drift /
  console build — AWF owns those after agent completion.

## PR

Title: `fix: clear stale awaiting-human attention during merge and non-human gate waits (#661, #663)`

Body: explain the decouple-episode-start-from-DB-flag fix (clearing the column
in the RESOLVED case is safe because the still-blocked fallback re-sets it every
poll via COALESCE, so the timer does not age) + the bounded-marker-TTL
resolved-vs-still-blocked distinction, and that both regressions
(`test_merge_blocker_fallback_keeps_attention_since_stable_across_polls` and
`test_merge_queue_wait_preserves_active_branch_protection_attention`) are
preserved. State `Fixes #661` and `Fixes #663`. Commit before exiting; the AWF
monitor handles the PR. Do not address review comments or merge.
