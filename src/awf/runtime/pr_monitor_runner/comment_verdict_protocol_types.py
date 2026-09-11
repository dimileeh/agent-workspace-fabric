"""Reason codes, verdict vocabularies and result types for the verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget;
every name here is re-exported (``X as X``) from ``comment_verdict``, which
remains the import surface for the rest of the package and for tests.

Nothing here performs I/O or imports a sibling runner module, so it is safe to
import from anywhere in ``pr_monitor_runner`` without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AGENT_VERDICT_PROTOCOL_VIOLATION = "AGENT_VERDICT_PROTOCOL_VIOLATION"
AGENT_FIXED_WITHOUT_EVIDENCE = "AGENT_FIXED_WITHOUT_EVIDENCE"
AGENT_NON_FIXED_WITH_MUTATION = "AGENT_NON_FIXED_WITH_MUTATION"

AgentVerdict = Literal["fix_committed", "false_positive", "defer", "needs_human"]
MonitorVerdict = Literal[
    "fix_committed",
    "false_positive",
    "defer",
    "needs_human",
    "agent_failed",
]
# Existing comment-state helpers consume the wider monitor value. Agent-produced
# results remain the narrower ``AgentVerdict`` below.
Verdict = MonitorVerdict


class AgentVerdictProtocolError(ValueError):
    """A safe, typed failure to satisfy or substantiate the verdict protocol."""

    def __init__(
        self,
        *,
        reason_code: str = AGENT_VERDICT_PROTOCOL_VIOLATION,
        message: str = "Agent output did not satisfy the AWF verdict protocol.",
    ) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class AgentVerdictExecutionError(RuntimeError):
    """Provider execution ended without a semantic agent verdict.

    ``reason`` / ``preserved_head_sha`` are set on the #932 timeout path, where
    the agent's commits are deliberately kept: callers surface the reason as the
    item's recorded ``agent_failed`` reason so the preserved HEAD is visible to
    the operator instead of silently discarded.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        reason: str | None = None,
        preserved_head_sha: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.reason = reason
        self.preserved_head_sha = preserved_head_sha
        super().__init__(reason or "Agent execution ended before AWF accepted a verdict.")


@dataclass(frozen=True)
class VerdictResult:
    verdict: AgentVerdict
    reason: str | None = None
    # True when this verdict deliberately keeps an unpushed local commit the
    # agent authored for the item (the #925 correction outcomes). Such a


@dataclass(frozen=True)
class MonitorVerdictResult:
    """Wider persisted monitor state for provider failures outside the protocol."""

    verdict: MonitorVerdict
    reason: str | None = None
    # Set on the #932 timeout path so callers can tell "the watchdog fired and
    # the work survived" from "the provider failed" without re-parsing prose.
    reason_code: str | None = None
    preserved_head_sha: str | None = None


_VERDICT_PROTOCOL_CORRECTION_SUFFIX = """

Your previous response did not satisfy AWF's machine-readable verdict protocol.
Complete the same review item. Then emit exactly one of these records as the
final non-empty stdout line, with a non-empty reason and no output after it:
AWF-VERDICT: FIXED: <reason>
AWF-VERDICT: FALSE POSITIVE: <reason>
AWF-VERDICT: DEFER: <reason>
AWF-VERDICT: NEEDS_HUMAN: <reason>
Do not decorate, indent, quote, fence, or otherwise wrap the record. Exit
immediately after emitting it.
""".rstrip()

_FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT = (
    "Your previous FIXED record could not be accepted because this review item "
    "made no new item-scoped Git change after its start commit. Do not repeat "
    "FIXED unless you make a contentful change for this item. If the issue is a "
    "duplicate of a different review item, or was already addressed by a commit "
    "made before this item started, choose FALSE POSITIVE and state that reason. "
    "A commit you already made for this review item does not count as such an "
    "earlier commit: do not cite it as the reason for FALSE POSITIVE or DEFER — "
    "repeat FIXED and describe that change instead (#925)."
)
