# Validation: Clear stale awaiting-human attention during merge and non-human gate waits (#661, #663)

Validates the implementation in `plans/CLEAR_STALE_AWAITING_HUMAN_ATTENTION_PLAN.md`
against the saved workspace plan at `docs/awf-plans/ws_c0848ad8288f4579852c60a1.md`.

## Plan traceability

| Plan item | Status | Evidence |
|---|---|---|
| Decouple episode-start surfacing from DB-flag presence | done | `_clear_stale_merge_attention` now clears the RESOLVED case (stale marker) and the still-blocked fallback re-sets the column every poll via COALESCE, so the timer does not age (`lifecycle.py:_clear_stale_merge_attention`). |
| Bounded marker TTL on `merge_block_attention` | done | `MonitorState.merge_block_attention_active(*, now, ttl_seconds)` + `mark_merge_block_attention(*, now)` + `MonitorConfig.merge_block_attention_ttl_seconds` (default 120s ~2× `poll_interval_seconds`). Legacy boolean `"1"` treated as fresh on first read. |
| #661: clear before pre-merge settle sleep + fast path | done | Single `_clear_stale_merge_attention` call at critical-section entry (`merge_loop.py` after `merge_critical_section_entered`), covers both paths. |
| #663: distinguish resolved-vs-still-blocked on gate-wait polls | done | `_clear_stale_merge_attention` passes `now` + configured TTL; stale marker ⇒ clear + proceed, fresh marker ⇒ preserve. |
| Preserve #661 regression | green | `test_merge_blocker_fallback_keeps_attention_since_stable_across_polls` — seed updated to stamp a fresh marker (realistic prior-fallback state); assertion unchanged (episode start stable). |
| Preserve #663 regression | green | `test_merge_queue_wait_preserves_active_branch_protection_attention` — marker stamped fresh (`now=datetime.now(UTC)`); assertion unchanged (attention preserved, timer stable). |
| Do NOT change `decide()` gate logic / #656/#660 grace | unchanged | No edits to `decide()` or the grace helpers. |
| Do NOT add forge re-check per poll | n/a | TTL chosen instead; no new forge calls. |
| Do NOT change COALESCE / column schema | unchanged | `set_workspace_attention` / `clear_workspace_attention` untouched. |
| Do NOT touch merge-method-preflight arm | unchanged | Preflight arm still records sticky `_merge_method_blocked_key`; no `mark_merge_block_attention` there. |
| Keep changes scoped / no new abstractions | done | No files split; marker methods + 1 config knob + 1 clear call. |

## Tests added (TDD red → green)

### `tests/unit/runtime/test_pr_monitor_state.py` (new)
Marker TTL unit tests — all 6 green:
- `test_mark_merge_block_attention_stamps_wall_clock_timestamp`
- `test_merge_block_attention_active_true_within_ttl`
- `test_merge_block_attention_active_false_after_ttl_drops_marker`
- `test_merge_block_attention_active_without_ttl_preserves_legacy_behavior`
- `test_merge_block_attention_active_legacy_boolean_marker_treated_as_fresh`
- `test_clear_merge_block_attention_drops_marker_idempotently`

### `tests/unit/runtime/test_pr_monitor_merge_failures.py`
- `test_resolved_human_wait_clears_attention_before_pre_merge_settle` (#661 —
  asserts the flag is `None` DURING the settle sleep via `_AttentionCheckingSleep`).
- `test_resolved_human_wait_clears_attention_on_fast_path_into_merge` (#661 fast
  path — `pre_merge_settle_seconds == 0`, merge succeeds, flag cleared at
  critical-section entry).
- `test_merge_blocker_fallback_keeps_attention_since_stable_across_polls` (#661
  regression — seed updated to stamp a fresh marker; assertion unchanged).

### `tests/unit/runtime/test_merge_queue_ordering.py`
- `test_resolved_branch_protection_marker_preserved_on_merge_queue_wait` (#663
  PRESERVE-WHILE-QUEUED — stale marker + merge-queue wait ⇒ flag PRESERVED, not
  aged out by TTL; operator decision on the #663 queue-wait tension).
- `test_resolved_branch_protection_marker_preserved_on_reviewer_settle_wait`
  (#663 PRESERVE-WHILE-QUEUED parity — stale marker + reviewer-settle wait ⇒
  flag PRESERVED).
- `test_resolved_branch_protection_marker_preserved_on_initial_grace_wait`
  (#663 PRESERVE-WHILE-QUEUED parity — stale marker + initial-grace wait ⇒
  flag PRESERVED).
- `test_merge_queue_wait_preserves_active_branch_protection_attention` (#663
  regression — marker stamped fresh; assertion unchanged).

## Focused validation run

AWF owns broad validation (full suite, `pytest --cov`, `--cov-fail-under=99`,
OpenAPI drift, console build); those were NOT run inside the agent phase. The
focused checks below cover only the touched files and adjacent merge-loop
behavior.

### Ruff (touched files)
```
uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor.py \
  src/awf/runtime/pr_monitor_runner/lifecycle.py \
  src/awf/runtime/pr_monitor_runner/merge_loop.py \
  src/awf/runtime/pr_monitor_runner/gates.py \
  tests/unit/runtime/test_pr_monitor_merge_failures.py \
  tests/unit/runtime/test_merge_queue_ordering.py \
  tests/unit/runtime/test_pr_monitor_state.py \
  tests/unit/runtime/test_pr_monitor_merge_attention.py
→ All checks passed!
```

### Mypy (touched src files)
```
uv run --python 3.12 --extra dev mypy \
  src/awf/runtime/pr_monitor.py \
  src/awf/runtime/pr_monitor_runner/lifecycle.py \
  src/awf/runtime/pr_monitor_runner/merge_loop.py \
  src/awf/runtime/pr_monitor_runner/gates.py
→ Success: no issues found in 4 source files
```

### Targeted pytest (touched test files + co-located regressions)
```
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_merge_failures.py \
  tests/unit/runtime/test_merge_queue_ordering.py \
  tests/unit/runtime/test_pr_monitor_state.py \
  tests/unit/runtime/test_pr_monitor_merge_attention.py -q
→ 56 passed in 94.80s
```

### Adjacent merge-loop suites (regression safety for the critical-section-entry clear)
```
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py \
  tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py \
  tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py \
  tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_011.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py \
  tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py \
  tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py \
  tests/unit/runtime/test_release_pr_monitor.py \
  tests/unit/runtime/test_merge_coordinator_runner.py \
  tests/unit/runtime/test_pr_monitor_merge_methods.py \
  tests/unit/runtime/test_pr_monitor_awaiting_required_checks.py \
  tests/unit/runtime/test_pr_monitor_manual_merge.py -q
→ 175 passed (combined; all green)
```

## Gaps / non-goals

- The full AWF/GitHub validation (full suite, `pytest --cov`,
  `--cov-fail-under=99`, OpenAPI drift check via
  `python scripts/generate_openapi.py --check`, console build/test) is managed
  by AWF after agent completion and was NOT run inside the agent phase per the
  workspace contract.
- PRESERVE-WHILE-QUEUED (operator decision on the #663 queue-wait tension):
  the pre-merge non-human gate waits (merge queue / reviewer settle / initial
  review grace) NEVER age the `merge_block_attention` marker out by TTL — the
  marker persists until a REAL signal (merge re-stamp / success / new commit)
  confirms resolution. The bounded false-positive (a resolved block still shows
  "awaiting human" until the queue clears) is an ACCEPTED limitation; no forge
  re-check is added (deferred to a follow-up). The critical-section-entry clear
  (the #661 path) keeps the TTL age-out so a marker already stale at entry
  (block resolved BEFORE the wait) is cleared once the monitor is actively
  merging.
- No `decide()` gate logic or #656/#660 grace work changed (per plan non-goals).
- No `set_workspace_attention` COALESCE semantics or `awaiting_human_since`
  column schema changed (per plan non-goals).
- Merge-method-preflight arm untouched (per plan non-goals).

## PR

Title: `fix: clear stale awaiting-human attention during merge and non-human gate waits (#661, #663)`

Body summary: The "awaiting human" attention flag stayed surfaced while the
monitor was NOT actually waiting on a human — during the pre-merge settle /
fast-path merge (#661) and during merge-queue / reviewer-settle / initial-grace
waits after a branch-protection block resolved externally (#663). Root: the
clear was blocked because the resolved case was indistinguishable from the
still-blocked case, and a naive clear would reset the genuinely-blocked
"awaiting human for N" timer.

Fix: decouple the clear decision from DB-flag presence by giving
`merge_block_attention` a wall-clock timestamp + bounded TTL, and apply
PRESERVE-WHILE-QUEUED: the pre-merge non-human gate waits (merge queue /
reviewer settle / initial review grace) NEVER age the marker out by TTL —
the marker persists until a REAL signal (merge re-stamp / success / new
commit) confirms resolution. The critical-section-entry clear (the #661 path)
keeps the TTL age-out so a marker already stale at entry (block resolved
BEFORE the wait) is cleared once the monitor is actively merging. The
still-blocked regression (`test_merge_queue_wait_preserves_active_branch_protection_attention`)
and the #661 stable-timer regression
(`test_merge_blocker_fallback_keeps_attention_since_stable_across_polls`) stay
green. The bounded false-positive (a resolved block still shows "awaiting
human" until the queue clears) is an ACCEPTED limitation; no forge re-check is
added (deferred to a follow-up).

Fixes #661, Fixes #663.
