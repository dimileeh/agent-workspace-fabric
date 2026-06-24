# PR #666 CI failure repair validation

## Plan reference

- `plans/PR666_CI_FIX_PLAN.md`

## Requirement status

- Branch discipline / no broad CI: **Complete** (focused checks only)
- Root cause diagnosed before any code: **Complete**
- Flaky-test TTL fix applied to the `cfXk` test: **Complete**
- Focused regression checks recorded: **Complete**
- Validation report recorded: **Complete**

## Root cause (recap)

`python-full-coverage` failed because a coverage shard intermittently failed
on `test_long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait`
(PRRT_kwDOSJAM6s6LcfXk). The test stamps a `merge_block_attention` marker
FRESH at poll entry (`state.mark_merge_block_attention()`) with
`merge_block_attention_ttl_seconds=1.0`, then the variable pre-coordinator
setup gap inside `runner._execute` (DB loads, gate checks, status fetch) can
exceed 1.0s under CI load. The critical-section-entry clear
(`merge_loop.py:296`) measures the marker against the coordinator-ENTRY
timestamp captured AFTER that gap (`merge_loop.py:291`), so a marker fresh at
test-entry is STALE at coordinator-entry → `awaiting_human_since` is cleared
→ the preservation assertion fails.

This is the same flaky pattern the prior commit (b98eb638) fixed for the
sibling `dM4X` test by bumping its TTL 1.0 → 30.0. The `cfXk` test was an
oversight — it stamps fresh-at-entry the same way but was left at 1.0.

## Fix applied

`tests/unit/runtime/test_pr_monitor_merge_attention.py`: bumped the `cfXk`
test's `merge_block_attention_ttl_seconds` from `1.0` to `30.0` and replaced
the stale "small TTL demonstrates post-wait reclassification" rationale with
the same setup-gap-absorption rationale used by the sibling `dM4X` and `a_SZ`
tests (both already 30.0). No production code change — the production
entry-time fix is already correct; this is a test-only flakiness fix.

The `test_stale_at_coordinator_entry_marker_still_cleared_after_long_wait`
test (line ~591, TTL=1.0) is intentionally left at 1.0: it stamps the marker
far in the past (datetime 2025-12-31), so the marker is unconditionally stale
at entry regardless of setup gap — the small TTL is correct and strengthens
the "must clear a stale marker" assertion.

## Evidence

### Targeted tests (focused — full AWF/GitHub gate managed by AWF post-agent)

Stress run of the fixed test (15x, looking for any flake):

```bash
for i in $(seq 1 15); do uv run --python 3.12 --extra dev pytest \
  "tests/unit/runtime/test_pr_monitor_merge_attention.py::test_long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait" \
  -p no:randomly --timeout=60 -q 2>&1 | tail -1; done
```

Observed: `1 passed` x15 (stable; previously also passed locally because the
local setup gap is ~0.01s — well under 1.0s — but under CI load the gap can
exceed 1.0s and flip the test; the 30.0s TTL absorbs any plausible gap).

Whole merge-attention file (3x):

```bash
for i in 1 2 3; do uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_merge_attention.py \
  -p no:randomly --timeout=180 -q 2>&1 | tail -1; done
```

Observed: `20 passed` x3.

### Lint / format / type (focused)

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_merge_attention.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/runtime/test_pr_monitor_merge_attention.py
```

Observed: `All checks passed!` / `1 file already formatted`.

mypy against the touched test file reports only pre-existing errors in
fixtures and unrelated test lines (319/598/1304/1312/1399/1405); the changed
lines (~723-735, the TTL comment + value) introduce no new errors. The CI
`lint-and-type` job runs `mypy` against `src/` only (`pyproject.toml`
`files = ["src/"]`), so test-file mypy noise does not affect CI.

### Coverage note

`merge_attention.py` is at 100% line+branch coverage from the
merge-attention test file alone (verified via a focused `coverage run` over
the merge-attention + merge-failures test files). No coverage gap was
introduced or left by this test-only change; the 99% gate is owned by AWF
post-agent and was not run locally per the no-broad-CI rule.

## Iteration

No further iteration needed — the root cause is a single overlooked
fresh-at-entry test left at a CI-fragile 1.0s TTL, now aligned with its two
sibling tests at 30.0s.
