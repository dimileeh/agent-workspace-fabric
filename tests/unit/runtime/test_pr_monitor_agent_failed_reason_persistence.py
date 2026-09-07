"""A timed-out review item keeps its reason code and preserved HEAD durably (#932).

``_agent_failed_result`` builds a ``MonitorVerdictResult`` carrying the watchdog
reason code and the preserved HEAD, but every ordinary review item narrows that
result before persisting state: ``_address_thread`` returns ``result.verdict``,
and the review-comment path only reaches ``_sync_needs_human_reason``, which
stores reasons for ``defer`` / ``needs_human`` alone. The durable record was then
a bare ``agent_failed`` — so after a restart, or for an operator reading monitor
state, the promised "your work survived, resume from here" reason was gone
(PRRT_kwDOSJAM6s6fz-6r).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdictExecutionError,
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comments import (
    _address_review_comment_result,
    _address_thread,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _agent_failed_reason_state_key,
    _clear_addressed_state_by_id,
)

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_THREAD_ID = "PRRT_timed_out"
_COMMENT_ID = "issue:5558086911"
_PRESERVED_REASON = (
    f"agent timed out ({AGENT_IDLE_TIMEOUT}); preserved work at {_PRESERVED_HEAD} "
    f"— retrying from the original item start {_ITEM_START_HEAD}"
)


class _ItemRunner(SimpleNamespace):
    """Minimal monitor seam: the verdict invocation returns whatever it is given."""

    def __init__(self, outcome: BaseException | VerdictResult | MonitorVerdictResult) -> None:
        super().__init__()
        self._outcome = outcome
        self._workspace_runtime_context = ""

    async def _resolve_task_tag(self, _workspace_id: str) -> str | None:
        return None

    async def _invoke_cli_for_verdict_result(
        self, **_kwargs: object
    ) -> VerdictResult | MonitorVerdictResult:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _timeout_error() -> AgentVerdictExecutionError:
    return AgentVerdictExecutionError(
        reason_code=AGENT_IDLE_TIMEOUT,
        reason=_PRESERVED_REASON,
        preserved_head_sha=_PRESERVED_HEAD,
    )


async def _address(
    runner: _ItemRunner,
    state: MonitorState,
) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=ReviewThread(
            thread_id=_THREAD_ID,
            path="src/awf/runtime/pr_monitor_runner/comments.py",
            line=388,
            body_excerpt="persist the preserved-work reason",
        ),
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        owned_paths=[],
        task_tag=None,
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
async def test_timed_out_thread_persists_its_reason_before_the_verdict_narrows() -> None:
    """The thread's durable record keeps the timeout code and preserved HEAD."""
    state = MonitorState()

    verdict = await _address(_ItemRunner(_timeout_error()), state)

    assert verdict == "agent_failed"
    assert state.threads_addressed_ids[_agent_failed_reason_state_key(_THREAD_ID)] == (
        _PRESERVED_REASON
    )


@pytest.mark.unit
async def test_thread_reason_is_dropped_once_a_real_verdict_answers_the_item() -> None:
    """A later verdict must not leave the previous timeout's reason standing."""
    state = MonitorState()
    await _address(_ItemRunner(_timeout_error()), state)

    verdict = await _address(_ItemRunner(VerdictResult(verdict="fix_committed")), state)

    assert verdict == "fix_committed"
    assert _agent_failed_reason_state_key(_THREAD_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_timed_out_review_comment_persists_its_reason_too() -> None:
    """The review-comment path narrows the same result and must record it first."""
    state = MonitorState()

    result = await _address_review_comment_result(
        _ItemRunner(_timeout_error()),  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        comment=ReviewComment(comment_id=_COMMENT_ID, body_excerpt="please fix this"),
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        owned_paths=[],
        task_tag=None,
        operation_start_head=_ITEM_START_HEAD,
    )

    assert result.verdict == "agent_failed"
    assert state.threads_addressed_ids[_agent_failed_reason_state_key(_COMMENT_ID)] == (
        _PRESERVED_REASON
    )


@pytest.mark.unit
async def test_reasonless_provider_failure_still_records_its_reason_code() -> None:
    """The non-timeout provider-failure raise carries a code but no prose reason."""
    state = MonitorState()

    verdict = await _address(
        _ItemRunner(AgentVerdictExecutionError(reason_code=AGENT_SERVICE_UNHEALTHY)),
        state,
    )

    assert verdict == "agent_failed"
    assert (
        AGENT_SERVICE_UNHEALTHY
        in state.threads_addressed_ids[_agent_failed_reason_state_key(_THREAD_ID)]
    )


@pytest.mark.unit
async def test_rolling_an_item_back_clears_its_stale_agent_failed_reason() -> None:
    """A rollback that re-opens the item must not keep the old failure reason."""
    state = MonitorState()
    await _address(_ItemRunner(_timeout_error()), state)

    _clear_addressed_state_by_id(state, _THREAD_ID)

    assert _agent_failed_reason_state_key(_THREAD_ID) not in state.threads_addressed_ids
