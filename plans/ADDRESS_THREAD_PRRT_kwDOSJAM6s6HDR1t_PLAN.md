# Address Thread PRRT_kwDOSJAM6s6HDR1t Plan

## Problem Statement And Scope

The PR monitor completion cleanup skips the legacy pre-GC auth overlay unmount
when a compose teardown callback is available. GC unmounts auth overlays only
for delete candidates, so an empty or non-deleting plan can leave the auth
overlay mounted after compose teardown handling.

Scope is limited to the completion filesystem cleanup path for this review
thread and focused regression coverage.

## Requirements Checklist

- Verify the reviewer claim against `src/awf/runtime/pr_monitor_runner/lifecycle.py`
  and GC compose teardown behavior.
- Add a regression test that proves auth overlay teardown still runs when the
  completion GC plan has no delete candidate.
- Keep compose teardown ordering intact: when compose context exists, auth
  overlay teardown must happen after GC has attempted compose teardown, not
  before.
- Preserve existing failure handling: auth overlay teardown failures are logged
  and do not block GC result handling.
- Avoid broad repository validation; record only focused checks because AWF owns
  broad validation after the agent phase.

## Implementation Steps

1. Add a focused monitor completion GC test for a no-candidate result with a
   compose teardown callback, asserting the auth overlay teardown is called.
2. Update `_gc_completed_workspace_filesystem` to perform a best-effort
   post-GC auth overlay teardown when compose context exists but GC did not
   include the workspace as a delete candidate.
3. Run the new focused test, then the surrounding focused test file or selected
   tests if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "empty_plan or auth_overlay"`
  - Passes and covers the new no-candidate auth overlay fallback.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
