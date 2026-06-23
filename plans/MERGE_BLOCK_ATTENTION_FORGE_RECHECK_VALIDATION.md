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
- Configured full coverage gate: `.awf/workspace.yml` declares
  `minimum_percent: 99` with command
  `uv run --python 3.12 --extra dev pytest --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99`.
- AWF-owned coverage evidence for this unpushed local head is not available
  inside the agent workspace:
  - no checked-in or temporary coverage artifact was present under `/workspace`
    or `/tmp`;
  - a read-only local control-plane DB lookup could not attach coverage
    provenance because this workspace DB has no `validation_runs` table;
  - `gh run list --repo dimileeh/agent-workspace-fabric --commit "$(git rev-parse HEAD)" --limit 20 --json ...`
    returned `[]`.

Full AWF/GitHub validation, the configured 99% coverage gate, and
CI-equivalent gates were not run inside the agent phase per the workspace
contract. No local document should be read as claiming that the 99% gate is
satisfied for this head; AWF/GitHub must produce that authoritative evidence
after the agent exits and the head is pushed.
