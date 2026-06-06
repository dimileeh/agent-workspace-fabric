---
name: pattern-shield-reawait-cancellation
description: Worker claim/operation finalizers use asyncio.shield + re-await loop on an orphan task; bug hunters misread this as a leak. It is intentional.
metadata:
  type: project
---

In `src/awf/control/worker/claims.py`, several `*_after_cancellation` helpers
(`_release_execution_claim_after_cancellation` ~L773, `_finish_monitor_recovery_operation_after_cancellation` ~L663)
use the idiom: `task = asyncio.create_task(inner)` then loop `await asyncio.shield(task)`
suppressing/catching `CancelledError` until `task.done()`.

**Why:** These run from a `finally` (e.g. `dispatch_methods.py:_safely_provision_claimed`)
while an external cancel (worker shutdown) is already propagating. The DB write must complete
despite repeated cancels. `asyncio.shield` does NOT cancel the inner orphan task when the
outer coroutine is cancelled — the task runs to completion; the loop re-shields each cancel.
The inner callee (`_release_execution_claim`) wraps its body in `try/except Exception` (swallows+logs),
so the orphan task can never finish with an exception — only normal completion, or
`CancelledError` if something calls `task.cancel()` directly (nothing does, except loop teardown).

**How to apply:** Bug hunters (e.g. Cursor Bugbot) flag these as "exits loop on done() without
re-awaiting, may pop in-memory state while DB claim never cleared." This is a FALSE POSITIVE.
The only path to the inner task ending cancelled is full event-loop/process teardown, where the
in-memory dict (`_execution_claim_epochs`) dies with the process anyway and the DB lease is
reclaimed by lease-expiry — the documented, intentional backstop (see dispatch_methods.py ~L303
"the next poll re-claims and retries"). Both the new and the accepted mirror helper share this
identical teardown residual, so neither is held to a stricter standard. Dismiss with High confidence.
