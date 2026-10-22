"""Shared helpers for review-thread disposition tests (#1018).

``seed_resolvable_disposition`` builds the complete disposition record a
resolvable verdict needs before AWF may reply and resolve; legacy runner tests
whose fake ``_address_thread`` returns a bare verdict use it to state that
contract explicitly. ``OrderedForge`` records replies and resolves in one call
log so tests can assert "reply, then resolve" ordering.
"""

from __future__ import annotations

from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    record_item_disposition,
)
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient

_DEFAULT_COMMIT = "c" * 40
_DEFAULT_ISSUE = "https://github.com/acme/widgets/issues/7"
_DEFAULT_REASON = "the reviewed code already handles this case"


def seed_resolvable_disposition(
    state: MonitorState,
    item_id: str,
    verdict: str,
    *,
    reason: str = _DEFAULT_REASON,
    commits: tuple[str, ...] = (_DEFAULT_COMMIT,),
    issue_url: str = _DEFAULT_ISSUE,
) -> ThreadDisposition:
    """Record a complete disposition for ``fix_committed`` / ``false_positive`` / ``defer``."""
    if verdict == "fix_committed":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="fixed",
            verdict=verdict,
            reason=reason,
            commits=commits,
        )
    elif verdict == "false_positive":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="false_positive",
            verdict=verdict,
            reason=reason,
        )
    elif verdict == "defer":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="deferred",
            verdict=verdict,
            reason=reason,
            issue_url=issue_url,
        )
    else:
        raise ValueError(f"{verdict!r} is not a resolvable verdict")
    record_item_disposition(state, disposition)
    return disposition


class OrderedForge(DefaultMergeMethodGitHubClient):
    """Record disposition replies and resolves in one ordered event log."""

    def __init__(
        self,
        runner: AsyncCommandRunner,
        *,
        reply_error: ForgeClientError | None = None,
        resolve_error: ForgeClientError | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(runner, **kwargs)
        self.events: list[tuple[str, ...]] = []
        self.reply_error = reply_error
        self.resolve_error = resolve_error

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        """Record the reply, or raise the configured forge fault."""
        if self.reply_error is not None:
            self.events.append(("reply_failed", thread_id))
            raise self.reply_error
        reply_id = await super().reply_to_thread(thread_id=thread_id, body=body)
        self.events.append(("reply", thread_id, body))
        return reply_id

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Record the resolve, or raise the configured forge fault."""
        if self.resolve_error is not None:
            self.events.append(("resolve_failed", thread_id))
            raise self.resolve_error
        self.events.append(("resolve", thread_id))

    def replies(self) -> list[tuple[str, str]]:
        """Return ``(thread_id, body)`` for every posted reply."""
        return [(event[1], event[2]) for event in self.events if event[0] == "reply"]
