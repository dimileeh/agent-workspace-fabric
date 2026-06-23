# PRRT_kwDOSJAM6s6LcL-G Plan

## Problem Statement and Scope

The review thread (`src/awf/runtime/pr_monitor_runner/lifecycle.py:483`, thread
`PRRT_kwDOSJAM6s6LcL-G`) reports that the "refresh on preserve" re-stamp in
`_clear_stale_merge_attention` is only in memory: when a still-blocked PR parks
on a non-human gate wait (merge queue / reviewer settle / initial review
grace) and the monitor is cancelled or restarted during that sleep, the DB
keeps the OLD marker timestamp. On the next poll the marker can exceed the TTL,
so `_clear_stale_merge_attention` clears `awaiting_human_since` and the
subsequent deterministic merge rejection re-stamps it — restarting the
human-wait timer even though the operator block never resolved.

The outer `run()` loop persists `state` only after `_execute` returns
(`runner.py:455`), so a cancel/restart during the post-preserve gate wait never
flushes the re-stamped marker. The branch-protection fallback's own
`mark_merge_block_attention` call at `merge_loop.py:1424` is paired with a
durable `_persist_state` (line 1425) for exactly this reason; the in-helper
re-stamp added by PRRT_kwDOSJAM6s6LbXWQ lacks that durability.

Scope is limited to the preserve branch of `_clear_stale_merge_attention` in
`src/awf/runtime/pr_monitor_runner/lifecycle.py`. No other behavior changes.

## Requirements Checklist

- Persist the re-stamped `merge_block_attention` marker to the DB row before
  returning from the preserve branch of `_clear_stale_merge_attention`, so a
  cancel/restart during the subsequent non-human gate wait cannot strand the
  old (pre-re-stamp) marker timestamp.
- Persist ONLY the marker key (merged onto the DB-persisted
  `monitor_threads_addressed`), never flushing the whole in-memory
  `MonitorState` — mirrors the established
  `_persist_forge_transient_retry_count` / `_clear_preserved_head_marker_durably`
  pattern, which avoids persisting unconfirmed in-flight verdicts.
- Preserve all existing behavior for the stale (clear) branch, the no-marker
  case, and the TTL-disabled (`<= 0`) case.
- Add a focused regression test asserting the marker is durable on the
  persisted row WITHOUT relying on the outer `_persist_state` flush (mirroring
  `test_terminal_directive_drop_clears_stale_preserved_marker_durably`).
- Run only targeted validation; leave broad AWF/GitHub validation to AWF
  after agent completion.

## Implementation Steps

1. Add a `_persist_merge_block_attention_durably` helper in
   `src/awf/runtime/pr_monitor_runner/lifecycle.py` next to the other durable
   single-key persist helpers. It opens a session, locks the row
   (`get_for_update`), reads the current `monitor_threads_addressed`, sets the
   `_MERGE_BLOCK_ATTENTION_STATE_KEY` to the re-stamped timestamp (taken from
   the in-memory `state` so the persisted value matches what
   `mark_merge_block_attention` already stamped), and commits. No-op when the
   row is gone or the in-memory marker is absent.
2. Wire it into the preserve branch of `_clear_stale_merge_attention` so the
   re-stamp is durable before the helper returns. Pass the re-stamped
   timestamp from `state` (read back from `threads_addressed_ids`) so the DB
   value is exactly the in-memory value.
3. Export the new helper on the runner via `mixins.py` (mirroring the other
   durable helpers) so tests can call it indirectly if needed.
4. Import `_MERGE_BLOCK_ATTENTION_STATE_KEY` in `lifecycle.py`.
5. Add a focused regression test in
   `tests/unit/runtime/test_merge_queue_ordering.py` (next to
   `test_clear_stale_merge_attention_restamps_preserved_marker`) that:
   - seeds a workspace + a FRESH marker,
   - monkeypatches `_persist_state` to raise (so the durable persist cannot
     rely on the outer flush),
   - calls `_clear_stale_merge_attention` with `now` inside the TTL,
   - asserts the persisted `monitor_threads_addressed` carries the re-stamped
     timestamp WITHOUT any `_persist_state` flush.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_clear_stale_merge_attention_restamps_preserved_marker_durably -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_merge_queue_ordering.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/lifecycle.py`
- Pass criteria: the new regression test passes, the existing
  `_clear_stale_merge_attention` / merge-queue / settle / grace tests still
  pass, and ruff/mypy are clean for the touched files.
