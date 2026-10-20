"""Sanitized agent stdout fixtures for #1074 (evidence decides, not stdout formatting).

Shape-identical to the incidents: F1 and F2 carry a ``FIXED`` record with glued
follow-up prose (F1 also keeps talking after it), F3 carries no record at all,
and F4 is a valid non-FIXED record followed by background-task chatter.
"""

from __future__ import annotations

F1 = """Detection no longer skips an open pending pair. A later pass still runs both checks.

`test_pending_pair_is_rechecked` is unchanged and passes, along with the three related checks.

AWF-VERDICT: FIXED: Detection re-runs both checks for open pending pairs instead of skipping them because a retry is queued.Detection now re-runs both checks when a pair is still pending. The queued retry no longer causes that pass to skip the pair.

The unchanged cache test and the three related checks all passed.
"""

F1_REASON = (
    "Detection re-runs both checks for open pending pairs instead of skipping them "
    "because a retry is queued.Detection now re-runs both checks when a pair is still "
    "pending. The queued retry no longer causes that pass to skip the pair."
)

F2 = """Saved retries now replay only while the channel is still requested. After a switch away, those rows stay unfinished and unclaimed.

AWF-VERDICT: FIXED: Pending retries are skipped unless the channel is still requested.The database connection attempt was stopped because it hung. The fix itself is already committed: saved retries are skipped once the channel is removed.
"""

F2_REASON = (
    "Pending retries are skipped unless the channel is still requested.The database "
    "connection attempt was stopped because it hung. The fix itself is already "
    "committed: saved retries are skipped once the channel is removed."
)

F3 = """The temporary database relay exited because this environment could not reach the published port. The route tests had already passed, and the retry fix is committed.
"""

F4 = """AWF-VERDICT: FALSE POSITIVE: The comment is an automated benchmark gate report with every tier passing and no actionable feedback.
Noted — `agentic-eval: success`. Still waiting on the test shards.
Shard 11 green. Shards 1–6 outstanding.
"""

F4_REASON = (
    "The comment is an automated benchmark gate report with every tier passing and "
    "no actionable feedback."
)
