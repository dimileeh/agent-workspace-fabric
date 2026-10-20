"""Park a review item on an unexpected error once its rollback succeeded (#1076).

Kept separate so ``comment_verdict`` stays under the first-party line budget;
``reraise_or_park_item`` is re-exported (``X as X``) from ``comment_verdict``.

#1020 and #1074/#1075 set one rule: a single review item must never kill the
monitor. Five handlers in the item protocol still broke it. Each one rolled the
item's unaccepted edits back and then re-raised the original error with a bare
``raise``. An untyped error (``OSError``, ``RuntimeError``, ``TimeoutError``, a
Git spawn failure, a repository or session error) has no handler between the
protocol and the monitor loop. So the batch never reached its end-of-batch push,
its accepted commits stayed local, and the workspace failed. After a successful
rollback the worktree is back at a known floor, so the failure belongs to the
item.

The sites (``ItemSite``):

* ``agent_invocation``: the agent run, its start probes and the post-exception
  mirror-hooks repair (``comment_verdict``);
* ``commit_sink``: the dirty-worktree sink and the evidence gates
  (``comment_verdict``);
* ``post_attempt_tip_probe``: the HEAD probe before the correction
  (``comment_verdict``);
* ``correction_end_head_probe``: ``read_correction_end_head``;
* ``unscoped_fix_evidence_probe``: ``guarded_unscoped_fix_evidence``, used by
  the correction-unscoped path and by ``park_unaccepted_correction``.

Each site calls ``reraise_or_park_item`` only after its rollback succeeded. A
failed rollback never gets here: the site raises its typed terminal error
first. A cause with a dedicated upstream handler is re-raised unchanged, so its
reason code still reaches that handler. With no known floor or no local
worktree, no rollback actually ran, so the cause is re-raised unchanged as well,
as in ``park_unaccepted_correction``. Any other cause raises
``ItemUnexpectedError`` chained to it. The protocol entry point
(``comment_verdict_entrypoint``) turns that signal into a ``needs_human`` item
with ``item_unexpected_error_result``. ``asyncio.CancelledError`` is a
``BaseException`` and never enters these handlers.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Literal, NoReturn

from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.forge_errors import ForgeClientError
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_UNEXPECTED_ERROR,
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.types import SINK_INFRASTRUCTURE_ERRORS

_log = get_logger(__name__)

ItemSite = Literal[
    "agent_invocation",
    "commit_sink",
    "post_attempt_tip_probe",
    "correction_end_head_probe",
    "unscoped_fix_evidence_probe",
]

# Every class (or a base) that an explicit ``except`` arm on the item's way out
# catches in ``comments``, ``operator_hints``, ``fix_cycle`` or ``loop``. Two
# arms there do not wrap the protocol and are deliberately not mirrored:
# ``comments``' ``ValueError`` guard around ``datetime.fromisoformat`` and
# ``fix_cycle``'s settle-then-re-raise ``BaseException`` around the start-head
# read. ``ForgeClientError`` reaches ``run()``'s bounded forge retry.
DEDICATED_UPSTREAM_ERRORS: tuple[type[Exception], ...] = (
    *SINK_INFRASTRUCTURE_ERRORS,
    AgentVerdictProtocolError,
    AgentVerdictExecutionError,
    ComposeExecCleanupError,
    ForgeClientError,
)

_MESSAGE_LIMIT = 240
_TRACEBACK_LIMIT = 4000


class ItemUnexpectedError(Exception):
    """Internal signal: the item's rollback succeeded, then an untyped error ended it.

    Raised only by ``reraise_or_park_item``, chained to the cause, and caught only
    by the protocol entry point. It never leaves the item protocol.
    """

    def __init__(self, *, site: ItemSite, rollback_floor_head: str) -> None:
        self.site = site
        self.rollback_floor_head = rollback_floor_head
        super().__init__(f"Unexpected error at {site} after a successful rollback.")


def reraise_or_park_item(
    exc: Exception,
    site: ItemSite,
    rollback_floor_head: str | None,
    worktree_path: Path,
    /,
) -> NoReturn:
    """Re-raise ``exc`` unchanged, or turn it into the item park signal.

    Call it from ``exc``'s own handler, and only after the rollback succeeded.
    """
    if (
        isinstance(exc, DEDICATED_UPSTREAM_ERRORS)
        or rollback_floor_head is None
        or not worktree_path.exists()
    ):
        raise exc
    raise ItemUnexpectedError(site=site, rollback_floor_head=rollback_floor_head) from exc


def item_unexpected_error_result(
    park: ItemUnexpectedError,
    *,
    workspace_id: str,
) -> VerdictResult:
    """Park the item ``needs_human``, naming the site, the error and the floor.

    The cause is redacted before it is bounded, so a cut cannot leave a token
    fragment behind. An untyped cause that still carries a ``reason_code`` keeps
    it in the warning and the reason. The warning also carries the cause's
    redacted traceback, which the old raise left in the worker log: the park must
    not hide which frame failed. It stays out of the reason, which is rendered
    into the PR's NotifyHuman comment.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_correction import _bounded

    cause = park.__cause__
    exc_type = type(cause).__name__
    message = " ".join(redact_secrets(str(cause)).split())[:_MESSAGE_LIMIT] or "<no message>"
    cause_reason_code = getattr(cause, "reason_code", None)
    # The tail keeps the innermost frame and the exception line.
    redacted_traceback = redact_secrets("".join(traceback.format_exception(cause)))
    _log.warning(
        "monitor.agent_verdict_item_unexpected_error_parked",
        workspace_id=workspace_id,
        reason_code=AGENT_VERDICT_ITEM_UNEXPECTED_ERROR,
        exc_type=exc_type,
        site=park.site,
        rollback_floor_head=park.rollback_floor_head,
        cause_reason_code=cause_reason_code,
        error=message,
        redacted_traceback=redacted_traceback[-_TRACEBACK_LIMIT:],
    )
    coded = f" ({cause_reason_code})" if cause_reason_code else ""
    return VerdictResult(
        verdict="needs_human",
        reason=_bounded(
            f"{AGENT_VERDICT_ITEM_UNEXPECTED_ERROR}: {exc_type}{coded} at {park.site} after "
            f"this item's unaccepted edits were rolled back to "
            f"{park.rollback_floor_head[:12]}; the item is parked for human review. "
            f"Error: {message}"
        ),
    )
