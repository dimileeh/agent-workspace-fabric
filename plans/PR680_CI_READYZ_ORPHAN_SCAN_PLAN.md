# PR680 CI Readiness Orphan Scan Fix Plan

## Problem statement and scope

PR #680 CI fails in `python-coverage-shards (1)` because two legacy `/readyz`
tests expect orphan Docker resources to make readiness fail with `503`, but PR
#680 intentionally makes `auto_cleanup_orphans` default-on. With reaping enabled,
`/readyz` still detects the orphan resources but reports
`ORPHANS_PRESENT_REAPING_ENABLED` and remains ready because the worker will reap
them automatically. The focused local repro matches CI.

While diagnosing the tests, the Docker inventory setup also proved
order-sensitive: the tests used FIFO `FakeCommandRunner` output even though
`/readyz` now runs Docker checks and Docker resource scans concurrently.

Scope is limited to the readiness tests/helpers needed to make the manual
cleanup-mode assertions explicit and deterministic under concurrent command
execution. Do not change production readiness behavior, CI config, or broad
validation settings.

## Requirements checklist

- [ ] Keep `/readyz` orphan-resource behavior expectations intact: detected
  orphan resources fail readiness when auto cleanup is disabled, and remain
  ready when auto cleanup is enabled.
- [ ] Replace order-sensitive queued Docker output in affected tests with a
  command-aware fake runner.
- [ ] Preserve existing assertions for reason codes, counts, examples, and
  cleanup readiness.
- [ ] Run focused local repro commands only; full AWF/GitHub validation remains
  managed after agent completion.
- [ ] Commit the scoped fix locally with a conventional CI-fix message.

## Implementation steps

1. Add a small command-aware runner helper in
   `tests/unit/api/test_health_parts/test_health_part_002.py` that returns
   Docker check and inventory output by argv pattern while recording calls.
2. Convert the orphan-resource readiness tests that rely on Docker inventory
   output from FIFO queueing to the new helper.
3. Make the two `503` orphan-resource tests explicitly disable
   `auto_cleanup_orphans`, preserving coverage for manual cleanup mode now that
   the default is reaping-enabled.
4. Run the failing two-test repro, then the containing health part test file if
   needed.
5. Write validation notes in
   `plans/PR680_CI_READYZ_ORPHAN_SCAN_VALIDATION.md`.
6. Commit the plan, validation, and test fix locally.

## Verification commands and pass criteria

- `uv run --python 3.12 pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_live_container_reports_leak_without_auto_cleanup tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_present_without_auto_cleanup_returns_503 -q`
  - Passes both tests.
- `uv run --python 3.12 pytest tests/unit/api/test_health_parts/test_health_part_002.py -q`
  - Passes the focused readiness test file.

Full AWF/GitHub CI validation, including the coverage aggregate gate, is
intentionally not run locally per the AWF workspace contract.
